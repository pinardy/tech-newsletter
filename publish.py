"""Write published editions to docs/data as static shards.

Design constraints this file answers to:

  * No Worker, no D1 — the web app reads plain JSON over HTTPS from Pages.
  * The reader picks 3 topics out of 20; they must not download the other 17.
    So the unit of transfer is one (date, topic) shard, not one date.
  * The manifest is rebuilt by scanning the directory, never by mutating a
    previous manifest. Derived state that can be recomputed should be, or a
    single failed run leaves you with a manifest that lies about disk.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import date as Date, datetime, timezone
from pathlib import Path
from typing import Iterable

from models import Cluster

DOCS_DATA = Path("docs/data")

# Weekly rollups live in their own date-space under the served tree. Same shard
# shape as a daily edition, so the reader loads them with the same code — only
# the prefix differs.
WEEKLY = "weekly"
SEARCH = "search"
# 2 adds the optional per-item `arc` object. Additive only, so a reader built
# for schema 1 ignores it and still renders.
#
# 3 is the first subtractive change, and only to the manifest: it drops three
# fields nothing ever read. `shards[date][topic].bytes`, the per-topic `dates`
# list, and `degraded: false` cost nothing each and everything together — the
# manifest is fetched on every cold load, and at full retention those three came
# to roughly 100KB of the ~136KB total. A shard payload is unchanged.
#
# 4 adds `weeks` and `weekly`, mirroring `dates` and `shards` for the weekly
# rollups. Additive: a reader that ignores them still renders every daily.
SCHEMA_VERSION = 4

# Retain this many days on the site. Older shards are deleted from docs/ but
# their raw items stay in data/items/*.jsonl, so a longer window is a rebuild
# away rather than a data loss.
RETAIN_DAYS = 120
# Roughly the same span in rollups. Kept separate so shortening the daily
# archive does not silently throw away half a year of weeklies.
RETAIN_WEEKS = 26


@dataclass(slots=True)
class Section:
    """A themed grouping assigned by the LLM during composition."""
    heading: str
    clusters: list[Cluster]


@dataclass(slots=True)
class Edition:
    date: Date
    topic: str
    label: str
    sections: list[Section]
    model: str | None = None
    degraded: bool = False      # True = fallback path, composed without an LLM


def _item_payload(cluster: Cluster, source_names: dict[str, str]) -> dict:
    rep = cluster.representative
    # Sources are ordered by first publication: whoever broke it leads.
    ordered = sorted(cluster.items, key=lambda i: i.published_at)
    return {
        "id": rep.id,
        "title": rep.title,
        "url": rep.url,
        "blurb": rep.blurb,
        "published_at": rep.published_at.isoformat(),
        # The corroboration signal. The UI renders one tally stroke per source,
        # so this list is the single most load-bearing field in the payload.
        "sources": [
            {"id": i.source_id, "name": source_names.get(i.source_id, i.source_id)}
            for i in dict.fromkeys(ordered)
        ],
        "source_count": len(cluster.sources),
        "score": round(cluster.score, 4),
        # Present only on stories published before and readmitted because they
        # gained a source: {"first_seen", "days", "was"}. The reader renders it
        # as "day 3 · was 2 src", which is the tally moving rather than a
        # single reading of it.
        **({"arc": cluster.arc} if getattr(cluster, "arc", None) else {}),
    }


def write_edition(
    edition: Edition,
    source_names: dict[str, str],
    root: Path = DOCS_DATA,
) -> Path:
    """Write one (date, topic) shard. Overwrites: rebuilding a date is safe."""
    day = root / edition.date.isoformat()
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"{edition.topic}.json"

    payload = {
        "schema": SCHEMA_VERSION,
        "date": edition.date.isoformat(),
        "topic": edition.topic,
        "label": edition.label,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": edition.model,
        "degraded": edition.degraded,
        "sections": [
            {
                "heading": s.heading,
                "items": [_item_payload(c, source_names) for c in s.clusters],
            }
            for s in edition.sections
        ],
    }

    # Separators without spaces: these files are fetched on mobile data.
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return path


DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"


def _scan(root: Path) -> tuple[list[str], dict[str, dict[str, dict]], dict[str, list[str]]]:
    """Read every date directory under `root` into manifest shape.

    Shared by the daily tree and the weekly one, which are the same shape in
    different date-spaces. Non-date directories — `weekly`, `search`, `feed` —
    do not match the glob, which is what keeps the two scans from eating
    each other.
    """
    dates: list[str] = []
    shards: dict[str, dict[str, dict]] = {}
    topic_dates: dict[str, list[str]] = {}

    for day in sorted(root.glob(DATE_GLOB), reverse=True):
        if not day.is_dir():
            continue
        day_shards: dict[str, dict] = {}
        for shard in sorted(day.glob("*.json")):
            topic = shard.stem
            try:
                data = json.loads(shard.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue  # a half-written shard must not poison the manifest
            count = sum(len(s.get("items", [])) for s in data.get("sections", []))
            if count == 0:
                continue
            # `degraded` only when it is true: the flag matters, the word
            # "false" repeated two thousand times does not.
            day_shards[topic] = {"items": count}
            if data.get("degraded"):
                day_shards[topic]["degraded"] = True
            topic_dates.setdefault(topic, []).append(day.name)
        if day_shards:
            dates.append(day.name)
            shards[day.name] = day_shards
    return dates, shards, topic_dates


def rebuild_manifest(
    topic_labels: dict[str, str],
    root: Path = DOCS_DATA,
) -> Path:
    """Scan the served tree and emit index.json.

    The app fetches this once on load, then lazily pulls only the shards for
    the topics the reader has selected. Everything here exists to let the app
    decide what NOT to fetch.
    """
    dates, shards, topic_dates = _scan(root)
    weeks, weekly, weekly_topics = _scan(root / WEEKLY)

    # A topic that only ever appeared in a rollup still needs a label, or the
    # weekly view would show a chip with no name.
    for topic, days in weekly_topics.items():
        topic_dates.setdefault(topic, [])

    manifest = {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latest": dates[0] if dates else None,
        "dates": dates,
        # No per-topic `dates` list. It restated what `shards` already says, once
        # per topic per retained day, and the reader derives the day it is
        # showing from `shards[date]` instead.
        "topics": {
            topic: {
                "label": topic_labels.get(topic, topic.title()),
                "total": sum(shards[d][topic]["items"] for d in days),
            }
            for topic, days in sorted(topic_dates.items())
        },
        "shards": shards,
        "weeks": weeks,
        "weekly": weekly,
    }

    path = root / "index.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    return path


def prune(retain_days: int = RETAIN_DAYS, root: Path = DOCS_DATA,
          retain_weeks: int = RETAIN_WEEKS) -> list[str]:
    """Drop shard directories beyond the retention window.

    Call before rebuild_manifest so the manifest reflects what survived.
    """
    removed = []
    for base, keep in ((root, retain_days), (root / WEEKLY, retain_weeks)):
        dirs = sorted((d for d in base.glob(DATE_GLOB) if d.is_dir()), reverse=True)
        for old in dirs[keep:]:
            shutil.rmtree(old)
            removed.append(str(old.relative_to(root)))
    return removed


def write_search_index(root: Path = DOCS_DATA) -> Path | None:
    """Emit a titles-only index of the whole archive, one file per month.

    The reader's own filter only ever sees the edition it has loaded, which
    makes 120 days of retention unsearchable — the thing you half-remember from
    last week is exactly what an archive is for.

    Titles and URLs only, no blurbs: the index exists to find the story, and the
    edition it belongs to can be opened for the rest. Split by month rather than
    written whole because the reader fetches this on demand and a single file
    covering full retention is megabytes.

    Built by scanning the shards on disk, not from the editions just composed,
    so a rebuild reindexes everything rather than only today.
    """
    out = root / SEARCH
    months: dict[str, dict[str, dict]] = {}

    for day in sorted(root.glob(DATE_GLOB)):
        if not day.is_dir():
            continue
        month = day.name[:7]
        bucket = months.setdefault(month, {})
        for shard in sorted(day.glob("*.json")):
            try:
                data = json.loads(shard.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for section in data.get("sections", []):
                for item in section.get("items", []):
                    # The same story is routed into several topics. File it once,
                    # under the topic that scored it highest — the same rule the
                    # reader uses to decide which section draws it.
                    prev = bucket.get(item["id"])
                    if prev and prev["_s"] >= item.get("score", 0):
                        continue
                    bucket[item["id"]] = {
                        "i": item["id"],
                        "t": item["title"],
                        "u": item["url"],
                        "d": data["date"],
                        "p": data["topic"],
                        "n": item.get("source_count", 1),
                        "_s": item.get("score", 0),
                    }

    if not months:
        return None

    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("*.json"):
        stale.unlink()          # a pruned month must not linger in the index

    catalogue = []
    for month, bucket in sorted(months.items(), reverse=True):
        entries = sorted(bucket.values(), key=lambda e: (e["d"], e["t"]), reverse=True)
        for e in entries:
            e.pop("_s", None)
        path = out / f"{month}.json"
        path.write_text(
            json.dumps({"month": month, "items": entries},
                       ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8")
        catalogue.append({"m": month, "items": len(entries),
                          "bytes": path.stat().st_size})

    path = out / "index.json"
    path.write_text(
        json.dumps({"schema": SCHEMA_VERSION,
                    "generated_at": datetime.now(timezone.utc)
                        .isoformat(timespec="seconds"),
                    "months": catalogue},
                   ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8")
    return path


def publish_all(
    editions: Iterable[Edition],
    source_names: dict[str, str],
    topic_labels: dict[str, str],
) -> dict:
    written = [write_edition(e, source_names) for e in editions]
    pruned = prune()
    manifest = rebuild_manifest(topic_labels)
    return {
        "shards_written": [str(p) for p in written],
        "pruned": pruned,
        "manifest": str(manifest),
    }
