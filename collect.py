"""Poll every source that is due, normalize, append to the durable store.

The seam this file draws is the one CHANGES.md argues for: ingestion knows
about feeds, newsletters and APIs; nothing downstream of `Item` does. Collect
runs on its own (three of four scheduled runs are collect-only) so that
re-polling never implies re-sending.

Store layout — raw store is deliberately *not* under `data/`, which is served
to the web:

    store/items/YYYY-MM-DD.jsonl   append-only, one Item per line
    store/state.json               per-source ETag / Last-Modified / last poll
    store/links.json               newsletter click-wrapper -> real URL
    store/health.json              last outcome per source
"""

from __future__ import annotations

import gzip
import json
import re
import ssl
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date as Date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

from models import Item, canonical_url, hash_token, utcnow

STORE = Path("store")
ITEMS_DIR = STORE / "items"
STATE_PATH = STORE / "state.json"
LINKS_PATH = STORE / "links.json"
HEALTH_PATH = STORE / "health.json"
SHIPPED_PATH = STORE / "shipped.json"

# Reddit rejects the default urllib UA outright; every other source ignores it.
USER_AGENT = "tech-digest/1.0 (+https://github.com/pinardy/tech-blog)"
TIMEOUT = 25

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ANCHOR = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)


# ── HTTP ────────────────────────────────────────────────────────────

def fetch(url: str, etag: str | None = None, modified: str | None = None):
    """Conditional GET. Returns (status, body_text, etag, last_modified).

    Most polls are 304s by design — four runs a day against sources that
    publish once a day means the useful work is in the exceptions.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/atom+xml, application/rss+xml, application/xml, "
                  "text/xml, application/json;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip",
    })
    if etag:
        req.add_header("If-None-Match", etag)
    if modified:
        req.add_header("If-Modified-Since", modified)

    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            charset = r.headers.get_content_charset() or "utf-8"
            return (r.status, raw.decode(charset, errors="replace"),
                    r.headers.get("ETag"), r.headers.get("Last-Modified"))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, "", etag, modified
        raise


BLURBS_PATH = STORE / "blurbs.json"

# Both attribute orders occur in the wild. Note the absence of re.S and the
# `[^>]*?` runs: without them the match walks out of the <meta> tag and on
# through the document, and you end up publishing a page's inline JavaScript
# as a story blurb.
_DESC_NAMES = r'(?:og:description|twitter:description|description)'
_META_DESC = re.compile(
    r'<meta[^>]*?(?:property|name)\s*=\s*["\']' + _DESC_NAMES + r'["\']'
    r'[^>]*?content\s*=\s*(["\'])(.*?)\1',
    re.I,
)
_META_DESC_REV = re.compile(
    r'<meta[^>]*?content\s*=\s*(["\'])(.*?)\1'
    r'[^>]*?(?:property|name)\s*=\s*["\']' + _DESC_NAMES + r'["\']',
    re.I,
)


def fetch_description(url: str) -> str | None:
    """Read a page's own meta description. Never invents text.

    Aggregators (HN, Lobsters, Reddit) and newsletter link-lists carry a title
    and nothing else, so without this most of the digest renders as bare
    headlines. The LLM composition step in CHANGES.md is what is meant to
    write these; until it exists, the publisher's own description is the only
    honest substitute.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Encoding": "gzip",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            if "html" not in (r.headers.get_content_type() or ""):
                return None
            raw = r.read(200_000)      # the head is all we need; do not slurp
            if r.headers.get("Content-Encoding") == "gzip":
                try:
                    raw = gzip.decompress(raw)
                except (OSError, EOFError):
                    pass               # truncated gzip stream — head may still decode
            html = raw.decode(r.headers.get_content_charset() or "utf-8",
                              errors="replace")
    except Exception:                  # noqa: BLE001
        return None

    m = _META_DESC.search(html) or _META_DESC_REV.search(html)
    if not m:
        return None
    text = _WS.sub(" ", unescape(m.group(2))).strip()

    # Belt and braces on top of the tag-bounded regex: a description is prose.
    # Anything carrying markup, braces or attribute syntax is a mis-parse, and
    # printing it is worse than printing nothing.
    if len(text) < 20 or any(c in text for c in "<>{}"):
        return None
    if re.search(r'\w+\s*=\s*["\']|function\s*\(|=>', text):
        return None
    return _blurb(text)


def fetch_descriptions(urls: list[str]) -> dict[str, str | None]:
    """Batch, with a permanent cache — an article's description never changes.

    Only ever called for items that made it into a published edition, so this
    is ~100 requests on a cold day and near zero thereafter, not one per item
    in the store.
    """
    cache = _load(BLURBS_PATH, {})
    missing = [u for u in dict.fromkeys(urls) if u not in cache]
    if missing:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for url, desc in zip(missing, pool.map(fetch_description, missing)):
                cache[url] = desc
        _save(BLURBS_PATH, cache)
    return cache


def resolve(url: str) -> str:
    """Follow a newsletter click-wrapper to the article it points at.

    Resolution is cached permanently: these redirects do not change, and a
    weekly newsletter would otherwise cost ~70 HEAD requests every run.
    """
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.url or url
    except Exception:
        return url


# ── Parsing ─────────────────────────────────────────────────────────

def _text(el) -> str:
    if el is None:
        return ""
    return _WS.sub(" ", unescape(_TAG.sub(" ", "".join(el.itertext())))).strip()


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_feed(body: str) -> list[dict]:
    """RSS 2.0 and Atom, into a common shape. Returns [] on unparseable XML."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []

    entries = []

    # RSS
    for item in root.findall("./channel/item"):
        link = _text(item.find("link"))
        if not link:
            guid = item.find("guid")
            if guid is not None and (guid.text or "").startswith("http"):
                link = guid.text.strip()
        body_html = "".join(
            e.text or "" for e in
            (item.find("content:encoded", _NS), item.find("description"))
            if e is not None
        )
        entries.append({
            "title": _text(item.find("title")),
            "link": link,
            "summary": _text(item.find("description")),
            "html": body_html,
            "date": _parse_date(_text(item.find("pubDate"))
                                or _text(item.find("dc:date", _NS))),
        })

    # Atom
    for entry in root.findall("atom:entry", _NS):
        link = ""
        for ln in entry.findall("atom:link", _NS):
            rel = ln.get("rel", "alternate")
            if rel == "alternate" and ln.get("href"):
                link = ln.get("href")
                break
        if not link:
            link = _text(entry.find("atom:id", _NS))
        content = entry.find("atom:content", _NS)
        summary = entry.find("atom:summary", _NS)
        entries.append({
            "title": _text(entry.find("atom:title", _NS)),
            "link": link,
            "summary": _text(summary if summary is not None else content),
            "html": "".join((content.itertext() if content is not None else [])),
            "date": _parse_date(_text(entry.find("atom:updated", _NS))
                                or _text(entry.find("atom:published", _NS))),
        })

    return [e for e in entries if e["title"] and e["link"]]


# Aggregators put a link stub where a description belongs: Lobsters emits
# "Comments", Reddit "[link] [comments] submitted by /u/...". Rendering that as
# a blurb is worse than rendering none, and treating it as absent lets the
# meta-description enrichment find the real thing.
_JUNK_BLURB = re.compile(
    r"^(comments?|\[link\]|\[comments\]|read more|continue reading|"
    r"submitted by\b|share this\b|permalink\b|discuss\b|via\b)",
    re.I,
)


def is_junk_blurb(text: str | None) -> bool:
    text = (text or "").strip()
    return len(text) < 25 or bool(_JUNK_BLURB.match(text))


def _blurb(text: str, limit: int = 240) -> str | None:
    """First sentence or two, trimmed. Empty blurbs are None, not ''."""
    text = _WS.sub(" ", text or "").strip()
    if is_junk_blurb(text):
        return None
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[:stop + 1] if stop > 80 else cut.rsplit(" ", 1)[0] + "…").strip()


# ── Adapters ────────────────────────────────────────────────────────

def _mk(source: dict, title: str, url: str, blurb, when, engagement=0,
        topics=()) -> Item | None:
    if not title or not url or not url.startswith("http"):
        return None
    canon = canonical_url(url)
    if not canon:
        return None
    # FNV, not hash(): Python salts hash() per process, and an item id that
    # changes between runs breaks any future "already shipped" suppression.
    return Item(
        id=f"{source['id']}:{hash_token(canon) % (10 ** 12):012d}",
        source_id=source["id"],
        title=_WS.sub(" ", unescape(title)).strip()[:220],
        url=url,
        canonical=canon,
        blurb=blurb,
        published_at=when,
        topics=tuple(topics),
        engagement=engagement,
    )


def adapter_feed(source: dict, body: str, router) -> list[Item]:
    out = []
    for e in parse_feed(body):
        when = e["date"] or utcnow()
        item = _mk(source, e["title"], e["link"], _blurb(e["summary"]), when,
                   topics=router(source, e["title"], e["summary"]))
        if item:
            out.append(item)
    return out


def adapter_newsletter(source: dict, body: str, router, link_cache: dict) -> list[Item]:
    """One issue == many stories.

    Cooper Press wraps every outbound link in a click tracker on its own
    domain, so the href in the feed is useless for clustering: JavaScript
    Weekly and Hacker News linking the same article agree on nothing until the
    wrapper is resolved. Resolution is the whole reason this adapter exists
    separately from `adapter_feed`.
    """
    issues = parse_feed(body)
    if not issues:
        return []

    host = (urlsplit(source["url"]).hostname or "").lower()
    candidates: list[tuple[str, str, datetime]] = []
    seen_wrappers: set[str] = set()

    for issue in issues[:2]:            # only the current and previous issue
        when = issue["date"] or utcnow()
        for href, label in _ANCHOR.findall(issue["html"] or ""):
            title = _WS.sub(" ", unescape(_TAG.sub("", label))).strip()
            # Link-list entries are titles; navigation chrome is short, and
            # "read online"/"unsubscribe" boilerplate is not a story.
            if len(title) < 18 or len(title.split()) < 3:
                continue
            if not href.startswith("http"):
                continue
            link_host = (urlsplit(href).hostname or "").lower().removeprefix("www.")
            # Two newsletter shapes: Cooper Press wraps every link in a
            # tracker on its own domain, SRE Weekly links straight out. Only
            # the wrapped form needs resolving; an unwrapped link that is
            # still on the publisher's own domain is issue navigation
            # ("read online", "unsubscribe", "issue 412"), never a story.
            if link_host == host.removeprefix("www.") or "cooperpress" in link_host:
                if "/link/" not in href:
                    continue
            if href in seen_wrappers:
                continue
            seen_wrappers.add(href)
            candidates.append((href, title, when))

    unresolved = [h for h, _, _ in candidates
                  if "/link/" in h and h not in link_cache]
    if unresolved:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for wrapper, target in zip(unresolved, pool.map(resolve, unresolved)):
                link_cache[wrapper] = target

    out = []
    for wrapper, title, when in candidates:
        target = link_cache.get(wrapper, wrapper)
        if (urlsplit(target).hostname or "").lower().removeprefix("www.") == host.removeprefix("www."):
            continue                    # wrapper never resolved off-site
        item = _mk(source, title, target, None, when,
                   topics=router(source, title, ""))
        if item:
            out.append(item)
    return out


def adapter_hn(source: dict, router, since: datetime) -> list[Item]:
    """Algolia, not a feed: points are the only engagement signal we get.

    `min_points` is applied server-side — filtering client-side would mean
    paging through the whole firehose to discard 95% of it.

    Unlike every other source this one is a time-range query, so a gap in
    polling is recoverable: `since` comes from the last successful poll, and
    on a cold start it reaches back a week. That matters more than it sounds.
    The weekly newsletters link articles from across the preceding week; if HN
    only ever covers the last 24h, the two never overlap and the corroboration
    tally — the entire point of merging sources — reads 1 for everything.
    """
    ts = int(since.timestamp())
    hits = []
    for page in range(5):           # 500 stories is far past a week at >=150 points
        url = (f"{source['url']}?tags=story"
               f"&numericFilters=created_at_i>{ts},points>={source.get('min_points', 150)}"
               f"&hitsPerPage=100&page={page}")
        status, body, _, _ = fetch(url)
        if status != 200:
            break
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            break
        batch = payload.get("hits", [])
        hits.extend(batch)
        if page >= payload.get("nbPages", 1) - 1 or not batch:
            break

    out = []
    for h in hits:
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        when = _parse_date(h.get("created_at")) or utcnow()
        title = h.get("title") or ""
        item = _mk(source, title, link, None, when,
                   engagement=int(h.get("points") or 0),
                   topics=router(source, title, ""))
        if item:
            out.append(item)
    return out


# ── Routing ─────────────────────────────────────────────────────────

def make_router(config: dict):
    """Fixed topics for most sources; keyword routing for `topics: auto`.

    An auto item can land in several topics — a Kubernetes CVE is both
    `security` and `kubernetes` — which is correct: the reader who picked only
    one of those still needs to see it.
    """
    # Word-boundary matching, not substring: `ai` otherwise fires on "said"
    # and "rust" on "trust", and a mis-routed item is worse than a missing one
    # because it displaces a correct item from a fixed topic budget.
    rules = {
        topic: [
            re.compile(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])")
            for k in keywords
        ]
        for topic, keywords in (config.get("routing") or {}).items()
    }

    def router(source: dict, title: str, blurb: str) -> tuple[str, ...]:
        declared = source.get("topics")
        if declared != "auto":
            return tuple(declared or ())
        hay = f" {title} {blurb or ''} ".lower()
        hits = [t for t, patterns in rules.items() if any(p.search(hay) for p in patterns)]
        return tuple(hits) if hits else ("programming",)

    return router


# ── Store ───────────────────────────────────────────────────────────

def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def append_items(items: list[Item], day: Date) -> int:
    """Append to today's shard, skipping anything already stored today.

    Dedup is per (source, canonical): the same source re-listing a story on a
    later poll is a re-sighting, not a new row.
    """
    ITEMS_DIR.mkdir(parents=True, exist_ok=True)
    path = ITEMS_DIR / f"{day.isoformat()}.jsonl"

    seen = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            seen.add((d["source_id"], d["canonical"]))

    fresh = []
    for item in items:
        key = (item.source_id, item.canonical)
        if key in seen:
            continue
        seen.add(key)
        fresh.append(item)

    if fresh:
        with path.open("a", encoding="utf-8") as fh:
            for item in fresh:
                fh.write(json.dumps(item.to_json(), ensure_ascii=False) + "\n")
    return len(fresh)


def load_shipped() -> dict:
    """canonical URL -> {"first", "last", "sources"} for everything published.

    This is what stops a seven-day rebuild window from republishing the same
    seven days every morning. Keyed on canonical URL rather than item id so a
    story keeps its history when a second source picks it up and the cluster's
    representative changes.
    """
    return _load(SHIPPED_PATH, {})


def save_shipped(shipped: dict, retain_days: int = 120) -> None:
    """Persist, dropping records past the site's retention window.

    Unbounded growth here would be slow rather than harmful, but a record
    older than the oldest shard suppresses a story no reader can still see.
    """
    cutoff = (utcnow().date() - timedelta(days=retain_days)).isoformat()
    _save(SHIPPED_PATH, {k: v for k, v in shipped.items()
                         if v.get("last", "") >= cutoff})


def load_items(days: int = 30, until: Date | None = None) -> list[Item]:
    """Read the last `days` shards back into memory. This is the 'rebuild'."""
    until = until or utcnow().date()
    out = []
    for offset in range(days):
        path = ITEMS_DIR / f"{(until - timedelta(days=offset)).isoformat()}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(Item.from_json(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue        # a half-written line must not fail the run
    return out


# ── Entry point ─────────────────────────────────────────────────────

def collect(config: dict, *, day: Date | None = None, force: bool = False) -> dict:
    day = day or utcnow().date()
    now = utcnow()
    state = _load(STATE_PATH, {})
    links = _load(LINKS_PATH, {})
    health = _load(HEALTH_PATH, {})
    router = make_router(config)
    defaults = config.get("defaults", {})

    collected: list[Item] = []
    summary = {"polled": 0, "skipped": 0, "failed": 0, "new_items": 0}

    for source in config["sources"]:
        sid = source["id"]
        prev = state.get(sid, {})
        cadence = source.get("cadence_minutes", defaults.get("cadence_minutes", 360))

        last = _parse_date(prev.get("last_polled"))
        if not force and last and now - last < timedelta(minutes=cadence):
            summary["skipped"] += 1
            continue

        try:
            if source["adapter"] == "hn":
                # Cold start reaches back a week; afterwards, only far enough
                # to close the gap since the last poll (plus an hour of slack,
                # since Actions cron drifts 5-15 minutes).
                since = max(last - timedelta(hours=1), now - timedelta(days=7)) \
                    if last else now - timedelta(days=7)
                items = adapter_hn(source, router, since)
                status, etag, modified = 200, None, None
            else:
                status, body, etag, modified = fetch(
                    source["url"], prev.get("etag"), prev.get("last_modified"))
                if status == 304:
                    items = []
                elif source["adapter"] == "newsletter":
                    items = adapter_newsletter(source, body, router, links)
                else:
                    items = adapter_feed(source, body, router)

            collected.extend(items)
            summary["polled"] += 1
            state[sid] = {
                "last_polled": now.isoformat(timespec="seconds"),
                "etag": etag,
                "last_modified": modified,
            }
            health[sid] = {
                "name": source["name"],
                "status": "not modified" if status == 304 else "ok",
                "items": len(items),
                "at": now.isoformat(timespec="seconds"),
            }
            print(f"  {sid:22s} {status}  {len(items):3d} items")

        except Exception as exc:                       # noqa: BLE001
            summary["failed"] += 1
            health[sid] = {
                "name": source["name"],
                "status": f"error: {type(exc).__name__}: {exc}"[:160],
                "items": 0,
                "at": now.isoformat(timespec="seconds"),
            }
            print(f"  {sid:22s} FAIL {type(exc).__name__}: {exc}")
            # One broken source out of thirty must not fail the run. It is
            # recorded in health.json and surfaced in the run summary instead.

    summary["new_items"] = append_items(collected, day)
    _save(STATE_PATH, state)
    _save(LINKS_PATH, links)
    _save(HEALTH_PATH, health)
    return summary
