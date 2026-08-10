"""Item identity, clustering and ranking. No I/O, no network, no LLM.

The two load-bearing pieces here are `canonical_url` and `cluster_items`.
Everything the app shows that no other reader shows — the corroboration tally —
is downstream of them: if two sources carrying the same story do not produce
byte-identical canonical URLs, the cluster splits and six sources render as
six separate one-stroke stories.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that identify *which* resource is being addressed, and so must
# survive stripping. Everything else in a query string is, empirically,
# tracking.
_SIGNIFICANT_PARAMS = {"v", "id", "p", "t", "page", "story_fbid", "articleid"}

_TRACKING_PREFIXES = ("utm_", "mc_", "at_", "pk_", "hsa_", "_hs")
_TRACKING_EXACT = {
    "ref", "referrer", "source", "src", "fbclid", "gclid", "igshid", "mkt_tok",
    "cmp", "campaign", "_bhlid", "guccounter", "share", "s", "smid", "guce",
    "unlocked_article_code", "giftcopy", "sh", "spm", "trk", "trkcampaign",
}

# Medium and Substack append an opaque id to the slug: .../my-post-3f9a12b4c7de
_MEDIUM_SLUG_HASH = re.compile(r"-[0-9a-f]{8,16}$")

_WORD = re.compile(r"[a-z0-9\+#\.]+")

# Dropped before hashing a title: they carry no discriminating signal but do
# vary between publishers, which is exactly what breaks near-dup detection.
_STOPWORDS = frozenset("""
a an the and or but of for to in on at by with from as is are was were be been
this that these those it its how why what when your you we our us new now more
than then vs via using use used make makes making just about into over under
""".split())


def canonical_url(url: str) -> str:
    """Reduce a URL to a form two independent sources will agree on."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()

    scheme = "https"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    # m. and amp. subdomains address the same document.
    if host.startswith("m."):
        host = host[2:]
    if host.startswith("amp."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"

    path = parts.path or "/"
    if path.endswith("/amp"):
        path = path[:-4]
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if host in ("medium.com", "netflixtechblog.com") or host.endswith(".medium.com"):
        path = _MEDIUM_SLUG_HASH.sub("", path)

    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() in _SIGNIFICANT_PARAMS
        and not k.lower().startswith(_TRACKING_PREFIXES)
        and k.lower() not in _TRACKING_EXACT
    ]
    query = urlencode(sorted(kept))

    # Fragments never address a different article, only a position within one.
    return urlunsplit((scheme, host, path, query, ""))


def _tokens(title: str) -> list[str]:
    return [w for w in _WORD.findall(title.lower())
            if w not in _STOPWORDS and len(w) > 1]


def hash_token(token: str) -> int:
    """FNV-1a. Stable across processes, unlike Python's salted hash()."""
    h = 0xCBF29CE484222325
    for byte in token.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def titles_match(a: str, b: str, *, jaccard: float = 0.8, min_tokens: int = 3) -> bool:
    """Do two titles describe the same story?

    This replaced a 64-bit SimHash with a Hamming threshold, which measured
    badly on real data. Over one day of items the distance histogram between
    *unrelated* headlines was 21 pairs at d=13 and 1,986 at d=20 — the noise
    floor swamps the signal, because a headline has nowhere near enough tokens
    for SimHash to be stable. Worse, no threshold separated the classes: at
    d=13 sat both a true duplicate ("Assembly Hall of Shame" / "Assembly Hall
    of Shame: Racing to the bottom of CPU performance") and obvious
    non-duplicates ("Ask HN: Who is hiring?" / "Ask HN: Who wants to be
    hired?", two unrelated AWS announcements).

    Token sets separate those cleanly, via two rules:

      * strict subset — one headline is the other truncated or un-subtitled.
        Catches the Assembly pair; rejects Ask HN, where each side has words
        the other lacks.
      * high Jaccard — near-identical wording. Ask HN scores 0.63 and is
        rejected; a version bump ("Node v26.6.0" / "Node v26.5.0") scores 0.6
        because the version token differs, which is the behaviour you want.

    `min_tokens` stops two-word titles ("Go 1.26") from swallowing every
    headline they prefix.
    """
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return False
    overlap = len(ta & tb)
    if min(len(ta), len(tb)) >= min_tokens and (ta <= tb or tb <= ta):
        return True
    union = len(ta | tb)
    return union > 0 and overlap / union >= jaccard


@dataclass(frozen=True, slots=True)
class Item:
    """One story as seen by one source. Frozen: clusters put these in sets."""
    id: str
    source_id: str
    title: str
    url: str
    canonical: str
    blurb: str | None
    published_at: datetime
    topics: tuple[str, ...] = ()
    engagement: int = 0          # HN points; 0 where the source exposes none

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "canonical": self.canonical,
            "blurb": self.blurb,
            "published_at": self.published_at.isoformat(),
            "topics": list(self.topics),
            "engagement": self.engagement,
        }

    @classmethod
    def from_json(cls, d: dict) -> "Item":
        return cls(
            id=d["id"],
            source_id=d["source_id"],
            title=d["title"],
            url=d["url"],
            canonical=d["canonical"],
            blurb=d.get("blurb"),
            published_at=datetime.fromisoformat(d["published_at"]),
            topics=tuple(d.get("topics", ())),
            engagement=int(d.get("engagement", 0)),
        )


@dataclass(slots=True)
class Cluster:
    """One story, as carried by one or more sources."""
    items: list[Item]
    score: float = 0.0
    # Set by the suppression pass when this story has been published before:
    # {"first_seen": "YYYY-MM-DD", "days": n, "was": prior_source_count}.
    # None means the reader has not been shown this story yet.
    arc: dict | None = None

    @property
    def representative(self) -> Item:
        """The item whose text we print.

        Prefer one that actually has a blurb: aggregators (HN, Lobsters,
        Reddit) and newsletter link-lists carry a title and nothing else, so
        without this preference a story corroborated by five sources renders
        with no description just because HN happened to sort first.
        """
        return max(
            self.items,
            key=lambda i: (bool(i.blurb), len(i.blurb or ""), -i.published_at.timestamp()),
        )

    @property
    def sources(self) -> set[str]:
        return {i.source_id for i in self.items}

    @property
    def published_at(self) -> datetime:
        return min(i.published_at for i in self.items)

    @property
    def engagement(self) -> int:
        return max((i.engagement for i in self.items), default=0)

    @property
    def topics(self) -> set[str]:
        return {t for i in self.items for t in i.topics}


def cluster_items(
    items: list[Item],
    *,
    jaccard: float = 0.8,
    min_tokens: int = 3,
) -> list[Cluster]:
    """Group items into stories: exact canonical URL first, then title match.

    At most one item survives per source per cluster — the earliest. The UI
    renders `len(sources)` tally strokes but prints the `sources` list, and
    the two must not disagree.
    """
    by_canonical: dict[str, list[Item]] = {}
    for item in items:
        by_canonical.setdefault(item.canonical or item.url, []).append(item)

    groups = list(by_canonical.values())

    # Second pass: merge groups whose titles describe the same story. O(n^2)
    # over groups, which is a few hundred per day — not worth an index.
    titles = [g[0].title for g in groups]
    merged_into: dict[int, int] = {}
    for i in range(len(groups)):
        if i in merged_into:
            continue
        for j in range(i + 1, len(groups)):
            if j in merged_into:
                continue
            if titles_match(titles[i], titles[j],
                            jaccard=jaccard, min_tokens=min_tokens):
                merged_into[j] = i

    final: dict[int, list[Item]] = {}
    for idx, group in enumerate(groups):
        root = idx
        while root in merged_into:
            root = merged_into[root]
        final.setdefault(root, []).extend(group)

    clusters = []
    for group in final.values():
        earliest_per_source: dict[str, Item] = {}
        for item in sorted(group, key=lambda i: i.published_at):
            earliest_per_source.setdefault(item.source_id, item)
        clusters.append(Cluster(items=list(earliest_per_source.values())))
    return clusters


def score_cluster(
    cluster: Cluster,
    weights: dict[str, float],
    *,
    now: datetime,
    half_life_hours: float = 36.0,
    corroboration_bonus: float = 0.6,
    interest: float = 1.0,
) -> float:
    """recency x source_weight x corroboration x log(engagement) x interest."""
    from math import log

    age_hours = max((now - cluster.published_at).total_seconds() / 3600.0, 0.0)
    recency = 0.5 ** (age_hours / half_life_hours)

    # The best source that carried it, not the average: one strong publisher
    # picking a story up is a stronger signal than three weak ones diluting it.
    weight = max((weights.get(i.source_id, 1.0) for i in cluster.items), default=1.0)

    corroboration = 1.0 + corroboration_bonus * (len(cluster.sources) - 1)
    engagement = 1.0 + log(1 + cluster.engagement) / 6.0

    return recency * weight * corroboration * engagement * interest


def select(
    clusters: list[Cluster],
    *,
    per_source_cap: int = 4,
    budget: int = 12,
) -> list[Cluster]:
    """Take the top `budget` clusters, letting no single source dominate.

    The cap counts a cluster against every source that carried it, so a
    corroborated story spends budget from several sources at once. That is
    intended: it is already the strongest item, and it should not also be the
    cheapest to include.
    """
    chosen: list[Cluster] = []
    used: dict[str, int] = {}
    for cluster in sorted(clusters, key=lambda c: c.score, reverse=True):
        if len(chosen) >= budget:
            break
        if all(used.get(s, 0) >= per_source_cap for s in cluster.sources):
            continue
        chosen.append(cluster)
        for s in cluster.sources:
            used[s] = used.get(s, 0) + 1
    return chosen


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
