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
# 2 adds the optional per-item `arc` object. Additive only, so a reader built
# for schema 1 ignores it and still renders.
#
# 3 is the first subtractive change, and only to the manifest: it drops three
# fields nothing ever read. `shards[date][topic].bytes`, the per-topic `dates`
# list, and `degraded: false` cost nothing each and everything together — the
# manifest is fetched on every cold load, and at full retention those three came
# to roughly 100KB of the ~136KB total. A shard payload is unchanged.
SCHEMA_VERSION = 3

# Retain this many days on the site. Older shards are deleted from docs/ but
# their raw items stay in data/items/*.jsonl, so a longer window is a rebuild
# away rather than a data loss.
RETAIN_DAYS = 120


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


def rebuild_manifest(
    topic_labels: dict[str, str],
    root: Path = DOCS_DATA,
) -> Path:
    """Scan docs/data and emit index.json.

    The app fetches this once on load, then lazily pulls only the shards for
    the topics the reader has selected. Everything here exists to let the app
    decide what NOT to fetch.
    """
    dates: list[str] = []
    shards: dict[str, dict[str, dict]] = {}
    topic_dates: dict[str, list[str]] = {}

    for day in sorted(root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"), reverse=True):
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
    }

    path = root / "index.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    return path


def prune(retain_days: int = RETAIN_DAYS, root: Path = DOCS_DATA) -> list[str]:
    """Drop shard directories beyond the retention window.

    Call before rebuild_manifest so the manifest reflects what survived.
    """
    days = sorted(
        (d for d in root.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]") if d.is_dir()),
        reverse=True,
    )
    removed = []
    for day in days[retain_days:]:
        shutil.rmtree(day)
        removed.append(day.name)
    return removed


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
