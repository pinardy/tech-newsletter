#!/usr/bin/env -S uv run --with pyyaml --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""CLI for the digest.

    uv run run.py collect            poll every source that is due
    uv run run.py collect --force    ignore per-source cadence
    uv run run.py publish            rebuild, rank, write data/ shards
    uv run run.py verify             assert every feed in sources.yaml parses
    uv run run.py health             last outcome per source

`collect` and `publish` are separate commands even though the scheduled run
does both: the seam is what lets you re-poll without re-publishing, and
backfill a missed day without re-sending it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date as Date
from pathlib import Path

import yaml

import collect as collector
from compose import build_editions, commit_shipped, publish as publish_editions
from models import utcnow

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "sources.yaml"
ENV_PATH = ROOT / ".env"


def load_dotenv(path: Path = ENV_PATH) -> int:
    """Read KEY=value lines from .env into the environment.

    Hand-rolled rather than a dependency: the file has at most three keys in
    it and the parsing rules that matter fit in twenty lines.

    The real environment always wins (`setdefault`, never assignment). That
    ordering is the load-bearing part — it means a stale `.env` left in a
    checkout can never shadow a secret injected by CI, and
    `OPENCODE_API_KEY=... uv run run.py publish` still overrides the file.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return 0

    count = 0
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()

        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key.replace("_", "").isalnum():
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            quote, value = value[0], value[1:-1]
            if quote == '"':
                value = (value.replace("\\n", "\n").replace("\\t", "\t")
                              .replace('\\"', '"').replace("\\\\", "\\"))
        else:
            # Unquoted values may carry a trailing comment; quoted ones may not.
            hash_at = value.find(" #")
            if hash_at != -1:
                value = value[:hash_at].rstrip()

        # `KEY=` with nothing after it is skipped rather than set to "". The
        # shipped .env.example carries exactly that for every key, so a
        # freshly copied file would otherwise report "loaded 3 variables" and
        # read as configured when nothing is. Empty never means anything here:
        # llm.configured() treats a blank key as absent either way.
        if value and key not in os.environ:
            os.environ[key] = value
            count += 1
    return count


def load_config() -> dict:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    defaults = config.get("defaults", {})
    for source in config["sources"]:
        source.setdefault("weight", defaults.get("weight", 1.0))
        source.setdefault("cadence_minutes", defaults.get("cadence_minutes", 360))
    return config


def cmd_collect(args) -> int:
    config = load_config()
    day = Date.fromisoformat(args.date) if args.date else utcnow().date()
    print(f"collect {day} — {len(config['sources'])} sources")
    summary = collector.collect(config, day=day, force=args.force)
    print(f"\npolled {summary['polled']}  skipped {summary['skipped']}  "
          f"failed {summary['failed']}  new items {summary['new_items']}")
    return 0


def cmd_publish(args) -> int:
    config = load_config()
    day = Date.fromisoformat(args.date) if args.date else utcnow().date()

    items = collector.load_items(days=args.window, until=day)
    if not items:
        print("store is empty — run `collect` first", file=sys.stderr)
        return 1
    print(f"rebuilt {len(items)} items from the last {args.window} days")

    wanted = ({t.strip() for t in args.topics.split(",") if t.strip()}
              if args.topics else None)
    editions = build_editions(items, config, day=day,
                              suppress=not args.no_suppress, only=wanted)
    if not editions:
        print("nothing met the publication threshold", file=sys.stderr)
        return 1

    for edition in editions:
        n = sum(len(s.clusters) for s in edition.sections)
        how = edition.model or "rule-based"
        print(f"  {edition.topic:12s} {n:3d} items in "
              f"{len(edition.sections)} sections  [{how}]")
        # A dry run exists to show what would be published; section headings
        # are the part that varies run to run once a model is composing them.
        if args.dry_run:
            for section in edition.sections:
                print(f"       § {section.heading}  ({len(section.clusters)})")

    if args.dry_run:
        print("\ndry run — nothing written")
        return 0

    result = publish_editions(editions, config)
    if not args.no_suppress:
        commit_shipped(editions, day)
    print(f"\nwrote {len(result['shards'])} shards + {result['manifest']}")
    print(f"feed: {result['feed']}")
    if result["pruned"]:
        print(f"pruned {len(result['pruned'])} day(s) past retention")
    return 0


def cmd_verify(_args) -> int:
    """Fetch every source and assert it parses. Exits non-zero on any failure.

    CHANGES.md flags the registry as written from memory; this is the check
    that keeps a dead URL from silently vanishing from the digest.
    """
    config = load_config()
    bad = []
    for source in config["sources"]:
        if source["adapter"] == "hn":
            print(f"  {source['id']:22s} skipped (API, not a feed)")
            continue
        try:
            status, body, _, _ = collector.fetch(source["url"])
            entries = collector.parse_feed(body)
            ok = status == 200 and len(entries) >= 1
            print(f"  {source['id']:22s} {status} {len(entries):3d} entries"
                  f"{'' if ok else '   <-- FAIL'}")
            if not ok:
                bad.append(source["id"])
        except Exception as exc:                       # noqa: BLE001
            print(f"  {source['id']:22s} FAIL {type(exc).__name__}: {exc}")
            bad.append(source["id"])
    if bad:
        print(f"\n{len(bad)} unusable: {', '.join(bad)}", file=sys.stderr)
        return 1
    print("\nall sources parse")
    return 0


def cmd_health(args) -> int:
    try:
        health = json.loads(collector.HEALTH_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("no health data yet", file=sys.stderr)
        return 1
    rows = sorted(health.items(), key=lambda kv: (kv[1]["status"] == "ok", kv[0]))
    if args.markdown:
        print("| Source | Status | Items | Last poll |")
        print("|---|---|---:|---|")
        for sid, h in rows:
            print(f"| {h['name']} | {h['status']} | {h['items']} | {h['at']} |")
    else:
        for sid, h in rows:
            print(f"  {sid:22s} {h['status']:14s} {h['items']:3d}  {h['at']}")
    return 0


def main() -> int:
    # Before anything reads os.environ. Count only — never echo a value.
    loaded = load_dotenv()
    if loaded:
        print(f"loaded {loaded} variable(s) from .env")

    parser = argparse.ArgumentParser(prog="run.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", help="poll sources into the store")
    p.add_argument("--date", help="edition date (YYYY-MM-DD); blank = today")
    p.add_argument("--force", action="store_true", help="ignore cadence")
    p.set_defaults(fn=cmd_collect)

    p = sub.add_parser("publish", help="build and write data/ shards")
    p.add_argument("--date", help="edition date (YYYY-MM-DD); blank = today")
    p.add_argument("--topics", help="comma-separated subset")
    p.add_argument("--window", type=int, default=7,
                   help="days of store to rebuild from (default 7)")
    p.add_argument("--no-suppress", action="store_true",
                   help="ignore the already-published store; rebuild a full "
                        "edition from the whole window")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_publish)

    p = sub.add_parser("verify", help="check every feed URL parses")
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("health", help="last outcome per source")
    p.add_argument("--markdown", action="store_true")
    p.set_defaults(fn=cmd_health)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
