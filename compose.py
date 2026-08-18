"""Turn the raw store into per-(date, topic) editions.

Three passes, in this order, and the order matters:

  suppress -> score -> select -> section

Suppression runs before selection so that a story the reader was already shown
does not consume a slot in today's budget. Get this backwards and the digest
publishes twelve items a day of which nine are yesterday's.
"""

from __future__ import annotations

import html
import re
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path

import llm
from collect import fetch_descriptions, is_junk_blurb, load_shipped, save_shipped
from models import Cluster, cluster_items, score_cluster, select, utcnow
from publish import (
    SEARCH, WEEKLY, Edition, Section, prune, rebuild_manifest,
    write_edition, write_search_index,
)

DATA = Path("data")

# Titles that announce a version rather than describe a story. Kept narrow on
# purpose: a false positive here buries a real story under "Releases".
_RELEASE = re.compile(
    r"(\bv?\d+\.\d+(\.\d+)?\b.*\b(released|release|out|available|ships)\b"
    r"|\b(released|releases|releasing|launch(es|ed)?|now available|"
    r"general availability|ga)\b.*\bv?\d+\.\d+"
    r"|^\S+\s+v?\d+\.\d+(\.\d+)?\s*$)",
    re.I,
)


# ── Clustering ──────────────────────────────────────────────────────

def cluster_globally(items, knobs: dict) -> list[Cluster]:
    """Cluster every item once, across all topics.

    This used to bucket by topic first and cluster inside each bucket, which
    made corroboration a within-topic question. Two sources that carried the
    same story but route to different topics could never meet:

        https://nextjs.org/blog/next-16-3    ← one canonical URL
          javascriptweekly -> javascript     ← published as "1 src"
          reactstatus      -> react          ← published again as "1 src"

    Identical URL, two outlets, and the tally read one both times — while the
    story consumed a slot in two budgets. Over the current window, clustering
    once lifts corroborated stories from 26 to 31 and collapses 574 cluster
    instances into 484 distinct ones. The rate matters less than the fact that
    the old answer was wrong: two sources carrying one URL is one story with
    two sources, whatever the routing says.
    """
    return cluster_items(
        items,
        jaccard=knobs.get("title_jaccard", 0.8),
        min_tokens=knobs.get("min_title_tokens", 3),
    )


def route_to_topics(clusters: list[Cluster]) -> dict[str, list[Cluster]]:
    """Fan clusters out to every topic their items carry.

    `Cluster.topics` is the union across items, so a story that HN routed to
    `javascript` and Frontend Focus routed to `frontend` now appears in both
    with one tally, rather than as two separate stories.

    The lists share Cluster objects. Callers score per topic — `interest` is a
    per-topic knob — so each must take its own copy before assigning `score`,
    or the last topic scored would overwrite what every earlier one published.
    """
    by_topic: dict[str, list[Cluster]] = {}
    for cluster in clusters:
        for topic in cluster.topics:
            by_topic.setdefault(topic, []).append(cluster)
    return by_topic


# ── Suppression and story arcs ──────────────────────────────────────

def apply_suppression(
    clusters: list[Cluster],
    shipped: dict,
    day: Date,
) -> list[Cluster]:
    """Drop stories already published, unless they have gained a source.

    "Gained a source" is the one readmission rule, and it is what turns
    suppression into a feature rather than just a filter: a story that was
    carried by two outlets on Monday and six by Thursday is genuinely news
    again, and comes back carrying its arc — first seen, and what the tally
    used to read.

    Re-running publish for a day that has already been published is safe.
    Records written by today's own run are ignored when deciding suppression
    (they are recognised by `last == today`), so a rebuild reproduces the same
    edition instead of erasing it.
    """
    today = day.isoformat()
    kept = []

    def arc_for(first_seen: str, was: int | None) -> dict | None:
        if not was or first_seen >= today:
            return None
        return {
            "first_seen": first_seen,
            "days": (day - Date.fromisoformat(first_seen)).days + 1,
            "was": was,
        }

    for cluster in clusters:
        prior = [shipped[i.canonical] for i in cluster.items
                 if i.canonical in shipped]
        if not prior:
            kept.append(cluster)
            continue

        first_seen = min(p.get("first", today) for p in prior)
        last_seen = max(p.get("last", "") for p in prior)

        if last_seen < today:
            # Shown on an earlier day. Readmit only if the tally has moved.
            was = max(p.get("sources", 0) for p in prior)
            if len(cluster.sources) <= was:
                continue
            cluster.arc = arc_for(first_seen, was)
        elif last_seen == today:
            # Rebuilding today's own edition. `prev` is the tally as it stood
            # *before* today, so the arc reproduces exactly what was published
            # rather than being recomputed against today's own record.
            cluster.arc = arc_for(
                first_seen,
                max((p.get("prev") or 0 for p in prior), default=0),
            )
        # else: a later edition exists (backfilling an older date). Its records
        # say nothing about what this day's reader has seen, so ignore them.

        kept.append(cluster)

    return kept


def commit_shipped(editions: list[Edition], day: Date) -> None:
    """Write every published story back to the suppression store.

    Deliberately separate from `build_editions` and called only after a real
    write. Recording during the build would mean `--dry-run` silently burns
    the whole edition: the dry run marks everything shipped, and the next real
    run suppresses all of it and publishes nothing.
    """
    shipped = load_shipped()
    today = day.isoformat()
    for edition in editions:
        for section in edition.sections:
            for cluster in section.clusters:
                first = (cluster.arc or {}).get("first_seen", today)
                count = len(cluster.sources)
                for item in cluster.items:
                    record = shipped.get(item.canonical)
                    if record is None:
                        shipped[item.canonical] = {
                            "first": first, "last": today,
                            "sources": count,
                            "prev": (cluster.arc or {}).get("was"),
                        }
                    elif record.get("last", "") < today:
                        # A genuinely new publication day: yesterday's count
                        # becomes `prev`, which is what the arc will quote.
                        shipped[item.canonical] = {
                            "first": min(record.get("first", first), first),
                            "last": today,
                            "sources": count,
                            "prev": record.get("sources"),
                        }
                    # last >= today: either a rebuild of this day (leave the
                    # record alone so the rebuild is reproducible) or a
                    # backfill behind a newer edition (never move `last` back).
    save_shipped(shipped)


# ── Sectioning ──────────────────────────────────────────────────────

def rule_based_sections(clusters: list[Cluster], topic: str) -> list[Section]:
    """The no-LLM fallback.

    The ordering encodes what this system knows that a single feed does not.
    Corroboration comes before recency because that is the whole thesis of
    merging thirty sources — if six outlets ran it, it leads.
    """
    remaining = list(clusters)

    lead = remaining[:2]
    remaining = remaining[2:]

    developing = [c for c in remaining if c.arc]
    taken = {id(c) for c in developing}
    remaining = [c for c in remaining if id(c) not in taken]

    corroborated = [c for c in remaining if len(c.sources) >= 2]
    remaining = [c for c in remaining if len(c.sources) < 2]

    # A `releases` topic is all releases; splitting it would leave one section
    # holding everything and another holding nothing.
    if topic == "releases":
        releases: list[Cluster] = []
    else:
        releases = [c for c in remaining if _RELEASE.search(c.representative.title)]
        taken = {id(c) for c in releases}
        remaining = [c for c in remaining if id(c) not in taken]

    candidates = [
        ("Top of the wire", lead),
        ("Still developing", developing),
        ("Carried by more than one source", corroborated),
        ("Releases and versions", releases),
        ("Also circulating", remaining),
    ]
    return [Section(heading=h, clusters=cs) for h, cs in candidates if cs]


def compose_sections(editions: list[Edition]) -> None:
    """Section every edition in place, with one LLM request for the whole day.

    Batched rather than per topic because the request count, not the prompt
    size, is what runs into a free-tier allowance: seventeen calls fired back
    to back exhausted it partway through a publish, where one call does not.
    Whatever the model does not return is sectioned by rule and marked
    degraded, per topic, so a partial reply costs only the topics it omitted.
    """
    batch = {e.topic: (e.label, e.sections[0].clusters) for e in editions}
    grouping = llm.compose_all(batch)
    settings = llm.configured()
    model = settings[2] if settings else None

    for edition in editions:
        chosen = edition.sections[0].clusters
        spec = grouping.get(edition.topic)
        if spec:
            edition.sections = [
                Section(heading=h, clusters=[chosen[i] for i in idx])
                for h, idx in spec
            ]
            edition.model = model
            edition.degraded = False
        else:
            edition.sections = rule_based_sections(chosen, edition.topic)
            edition.model = None
            edition.degraded = True


# ── Blurbs ──────────────────────────────────────────────────────────

def enrich_blurbs(editions: list[Edition]) -> int:
    """Fill empty blurbs from each article's own meta description.

    Runs once across every edition rather than per topic: the same story
    routed into three topics is three distinct Cluster objects pointing at one
    URL, and it should cost one request.

    `is_junk_blurb` is applied here as well as at collect time so that rows
    already in the store, written before a filter existed, self-heal on the
    next publish rather than needing the JSONL rewritten.
    """
    from dataclasses import replace

    needy = [
        cluster
        for edition in editions
        for section in edition.sections
        for cluster in section.clusters
        if is_junk_blurb(cluster.representative.blurb)
    ]
    if not needy:
        return 0

    found = fetch_descriptions([c.representative.url for c in needy])
    filled = 0
    for cluster in needy:
        rep = cluster.representative
        desc = found.get(rep.url)
        if desc == rep.blurb:
            continue
        # No description found and the existing one is junk: print nothing.
        cluster.items = [replace(i, blurb=desc or None) if i is rep else i
                         for i in cluster.items]
        filled += bool(desc)
    return filled


# ── Build ───────────────────────────────────────────────────────────

def build_editions(
    items,
    config: dict,
    *,
    day: Date | None = None,
    suppress: bool = True,
    only: set[str] | None = None,
) -> list[Edition]:
    """Cluster, suppress, score, select and section one edition per topic.

    `only` restricts which topics are built at all. It is applied here rather
    than to the finished list because sectioning is the one step that costs
    money: filtering afterwards would bill an LLM call for all seventeen
    topics in order to publish one.
    """
    day = day or utcnow().date()
    now = utcnow()
    knobs = config.get("ranking", {})
    max_age = timedelta(hours=knobs.get("max_age_hours", 168))
    weights = {s["id"]: s.get("weight", 1.0) for s in config["sources"]}
    topic_meta = config.get("topics", {})
    shipped = load_shipped() if suppress else {}

    fresh = [i for i in items if now - i.published_at <= max_age]

    clusters = cluster_globally(fresh, knobs)
    if suppress:
        clusters = apply_suppression(clusters, shipped, day)

    editions = []
    for topic, shared in sorted(route_to_topics(clusters).items()):
        if only and topic not in only:
            continue
        meta = topic_meta.get(topic, {})
        topic_clusters = [Cluster(items=c.items, arc=c.arc) for c in shared]

        for cluster in topic_clusters:
            cluster.score = score_cluster(
                cluster, weights,
                now=now,
                half_life_hours=knobs.get("half_life_hours", 36),
                corroboration_bonus=knobs.get("corroboration_bonus", 0.6),
                interest=meta.get("interest", 1.0),
            )
        chosen = select(
            topic_clusters,
            per_source_cap=knobs.get("per_source_cap", 6),
            budget=knobs.get("topic_budget", 12),
        )
        if len(chosen) < knobs.get("min_topic_items", 2):
            continue

        editions.append(Edition(
            date=day,
            topic=topic,
            label=meta.get("label", topic.title()),
            sections=[],            # filled below, after blurb enrichment
            model=None,
            degraded=True,
        ))
        editions[-1].sections = [Section(heading="", clusters=chosen)]

    # Enrich before sectioning: the model sees only titles, but the rule-based
    # path splits on release-shaped titles and both want final text in place.
    filled = enrich_blurbs(editions)
    if filled:
        print(f"filled {filled} blurbs from publisher meta descriptions")

    compose_sections(editions)
    return editions


# ── Weekly rollup ───────────────────────────────────────────────────

def weekly_sections(clusters: list[Cluster]) -> list[Section]:
    """Sections for a rollup, which wants a different shape to a day.

    A daily edition leads on what is new. A week already knows what mattered,
    so it leads on what more than one outlet carried — which is also the only
    window where that question has a useful answer: over a single day almost
    nothing has been corroborated yet, and over seven days the tally has had
    time to move.
    """
    corroborated = [c for c in clusters if len(c.sources) >= 2]
    rest = [c for c in clusters if len(c.sources) < 2]
    candidates = [
        ("Carried by more than one source", corroborated),
        ("Also this week", rest),
    ]
    return [Section(heading=h, clusters=cs) for h, cs in candidates if cs]


def build_weekly(
    items,
    config: dict,
    *,
    week_end: Date | None = None,
    only: set[str] | None = None,
) -> list[Edition]:
    """One rollup per topic across the whole window, ignoring suppression.

    Suppression is what makes a daily edition worth reading twice, and exactly
    what a rollup must not do: every story in the week has already been
    published, so filtering on that would produce nothing at all.

    Sectioned by rule, never by model. A rollup re-selects stories the daily
    editions already composed, so spending a request on it buys new headings for
    old news — and the request count is the thing that runs into the free-tier
    allowance. `degraded` stays false because this is the intended path here,
    not a fallback.
    """
    week_end = week_end or utcnow().date()
    now = utcnow()
    knobs = config.get("ranking", {})
    weights = {s["id"]: s.get("weight", 1.0) for s in config["sources"]}
    topic_meta = config.get("topics", {})

    clusters = cluster_globally(items, knobs)

    editions = []
    for topic, shared in sorted(route_to_topics(clusters).items()):
        if only and topic not in only:
            continue
        meta = topic_meta.get(topic, {})
        clusters_for_topic = [Cluster(items=c.items, arc=c.arc) for c in shared]
        for cluster in clusters_for_topic:
            cluster.score = score_cluster(
                cluster, weights,
                now=now,
                # Decay is what keeps a daily edition current, and it is wrong
                # here: a rollup is asking which story of the week mattered, not
                # which is freshest. A half-life spanning the window flattens it
                # to near enough no decay at all.
                half_life_hours=knobs.get("weekly_half_life_hours", 24 * 30),
                corroboration_bonus=knobs.get("corroboration_bonus", 0.6),
                interest=meta.get("interest", 1.0),
            )
        # Corroboration first, score second: the rollup exists to surface what
        # several outlets carried, and ordering by score alone buries a
        # two-source story under a fresher single-source one.
        clusters_for_topic.sort(key=lambda c: (len(c.sources), c.score), reverse=True)
        chosen = select(
            clusters_for_topic,
            per_source_cap=knobs.get("weekly_per_source_cap", 4),
            budget=knobs.get("weekly_topic_budget", 8),
        )
        if len(chosen) < knobs.get("min_topic_items", 2):
            continue

        editions.append(Edition(
            date=week_end,
            topic=topic,
            label=meta.get("label", topic.title()),
            sections=weekly_sections(chosen),
            model=None,
            degraded=False,
        ))
    return editions


def publish_weekly(editions: list[Edition], config: dict,
                   root: Path = DATA) -> dict:
    """Write rollup shards and rebuild the manifest around them.

    Never calls commit_shipped: a rollup republishes stories on purpose, and
    recording them again would suppress them from the dailies that follow.
    """
    weekly_root = root / WEEKLY
    weekly_root.mkdir(parents=True, exist_ok=True)
    source_names = {s["id"]: s["name"] for s in config["sources"]}
    topic_labels = {t: m.get("label", t.title())
                    for t, m in config.get("topics", {}).items()}

    written = [write_edition(e, source_names, root=weekly_root) for e in editions]
    pruned = prune(root=root)
    manifest = rebuild_manifest(topic_labels, root=root)
    return {
        "shards": [str(p) for p in written],
        "pruned": pruned,
        "manifest": str(manifest),
    }


# ── Outputs ─────────────────────────────────────────────────────────

def _rfc822(dt: datetime) -> str:
    from email.utils import format_datetime
    return format_datetime(dt.astimezone(timezone.utc))


def _feed_items(editions: list[Edition]) -> list[str]:
    items = []
    for edition in sorted(editions, key=lambda e: e.topic):
        for section in edition.sections:
            for cluster in section.clusters:
                rep = cluster.representative
                names = ", ".join(sorted(cluster.sources))
                tally = f"{len(cluster.sources)} source" \
                        f"{'s' if len(cluster.sources) != 1 else ''}"
                desc = f"{rep.blurb + ' ' if rep.blurb else ''}({tally}: {names})"
                items.append(
                    "<item>"
                    f"<title>{html.escape(rep.title)}</title>"
                    f"<link>{html.escape(rep.url)}</link>"
                    f"<guid isPermaLink=\"false\">{html.escape(rep.id)}</guid>"
                    f"<category>{html.escape(edition.label)}</category>"
                    f"<pubDate>{_rfc822(rep.published_at)}</pubDate>"
                    f"<description>{html.escape(desc)}</description>"
                    "</item>"
                )
    return items


def _channel(title: str, description: str, site: str, items: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        f"<title>{html.escape(title)}</title>"
        f"<link>{html.escape(site or 'https://example.invalid/')}</link>"
        f"<description>{html.escape(description)}</description>"
        f"<lastBuildDate>{_rfc822(utcnow())}</lastBuildDate>"
        "<language>en</language>"
        + "".join(items) +
        "</channel></rss>"
    )


def write_feed(editions: list[Edition], root: Path = DATA,
               site: str = "") -> Path:
    """RSS 2.0 of one day, every topic, so the digest is itself subscribable.

    A digest that merges thirty feeds and then cannot be subscribed to is an
    odd thing to build. One item per story, the topic as its category, and the
    corroboration count in the description because that is the part no other
    feed can give you.

    Also written per topic. The combined feed carries all seventeen at once, so
    someone who wants Rust gets everything — which is the same firehose problem
    this project exists to solve, reintroduced at the subscription layer.
    """
    path = root / "feed.xml"
    path.write_text(_channel(
        "Wire — tech digest",
        "One digest a day, merged from thirty sources.",
        site, _feed_items(editions)), encoding="utf-8")

    per_topic = root / "feed"
    per_topic.mkdir(parents=True, exist_ok=True)
    live = set()
    for edition in editions:
        live.add(f"{edition.topic}.xml")
        (per_topic / f"{edition.topic}.xml").write_text(_channel(
            f"Wire — {edition.label}",
            f"{edition.label} from thirty sources, one digest a day.",
            site, _feed_items([edition])), encoding="utf-8")
    # A topic that published nothing today keeps its last feed rather than
    # having it emptied: an RSS reader treats a vanished item list as nothing
    # new, but a 404 as a broken subscription.
    return path


def write_health(root: Path = DATA) -> Path | None:
    """Copy source health into the served tree for the status page.

    `store/` holds raw collector state and is not something to serve; this is
    the reader-facing subset, regenerated each publish.
    """
    from collect import HEALTH_PATH
    import json

    try:
        health = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    payload = {
        "generated_at": utcnow().isoformat(timespec="seconds"),
        "sources": [
            {"id": sid, "name": h.get("name", sid), "status": h.get("status", "?"),
             "items": h.get("items", 0), "at": h.get("at")}
            for sid, h in sorted(health.items())
        ],
    }
    path = root / "health.json"
    path.write_text(json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":")), encoding="utf-8")
    return path


def publish(editions: list[Edition], config: dict, root: Path = DATA) -> dict:
    """Write shards, prune the retention window, rebuild the manifest.

    Order matters: prune before rebuild, or the manifest describes directories
    that are no longer on disk.
    """
    root.mkdir(parents=True, exist_ok=True)
    source_names = {s["id"]: s["name"] for s in config["sources"]}
    topic_labels = {t: m.get("label", t.title())
                    for t, m in config.get("topics", {}).items()}

    written = [write_edition(e, source_names, root=root) for e in editions]
    pruned = prune(root=root)
    manifest = rebuild_manifest(topic_labels, root=root)
    feed = write_feed(editions, root=root, site=config.get("site", ""))
    # After pruning, so a month that has just left the retention window leaves
    # the index with it rather than pointing at shards that are gone.
    search = write_search_index(root=root)
    write_health(root=root)
    return {
        "shards": [str(p) for p in written],
        "pruned": pruned,
        "manifest": str(manifest),
        "feed": str(feed),
        "search": str(search) if search else None,
    }
