# Wire

One digest a day, merged from 27 sources across 17 topics. Free to run: GitHub
Actions on a schedule, a static PWA on Pages, no server and no database.

The thing it does that a feed reader doesn't is count **corroboration**. Every
story is clustered across sources, and the reader draws one tally stroke per
independent outlet that carried it — struck through in fives — so *"six
outlets ran this"* is legible as a shape before you read the headline.

```
sources.yaml
    │
    ▼
[collect]   per-source cadence, conditional GET (ETag / If-Modified-Since)
    │
[normalize] every adapter emits Item — nothing downstream knows what a feed is
    │
    ▼
store/items/YYYY-MM-DD.jsonl        ← the durable store (append-only, in repo)
    │
[cluster]   canonical URL, then token-set title matching
    │
[suppress]  drop anything already published, unless it gained a source
    │
[rank]      arithmetic only — no LLM
    │
[compose]   section headings, one LLM call per (date, topic), optional
    │
    ▼
data/{date}/{topic}.json  +  data/index.json  +  data/feed.xml
```

## Quick start

Needs [uv](https://docs.astral.sh/uv/). No other setup — `run.py` carries a
PEP-723 header, so its one dependency installs on first run.

```bash
uv run run.py verify     # check every feed URL still parses
uv run run.py collect    # poll every source that is due
uv run run.py publish    # rank, select, section, write data/
```

Then serve the directory and open it:

```bash
python3 -m http.server 8000    # http://localhost:8000
```

`collect` and `publish` are separate commands even though the scheduled run
does both. The seam is what lets you re-poll without re-publishing, and
backfill a missed day without re-sending it.

## Commands

| Command | What it does |
|---|---|
| `run.py collect` | Poll every source past its cadence, append to the store. `--force` ignores cadence, `--date` sets the store shard. |
| `run.py publish` | Rebuild from the store, cluster, suppress, rank, select, section, write `data/`. `--date`, `--topics`, `--window`, `--dry-run`, `--no-suppress`. |
| `run.py verify` | Fetch every source and assert it parses. Exits non-zero on any failure. |
| `run.py health` | Last poll outcome per source. `--markdown` for the Actions run summary. |

## Configuration

Everything tunable lives in **`sources.yaml`**. Adding a source is a config
edit, never a code change.

- **27 sources** — 9 newsletters, 17 feeds, 1 API (Hacker News via Algolia).
- **17 topics.** Most sources declare theirs; 5 aggregators use
  `topics: auto` and are routed by the keyword rules at the bottom of the
  file. That routing is what lets a Hacker News thread corroborate a
  JavaScript Weekly link.
- **`ranking:`** holds the scoring knobs. Score is
  `recency × source_weight × corroboration × log(engagement) × interest`,
  then a per-source cap, then a fixed budget per topic. Deliberately
  arithmetic: its whole purpose is bounding cost as source count grows, which
  an LLM at that stage would defeat.

### Optional: LLM section headings

Copy `.env.example` to `.env` and set `OPENCODE_API_KEY`. The default endpoint
is Gemini's OpenAI-compatible API, so a key from
[ai.google.dev](https://ai.google.dev) is enough; any OpenAI-compatible
`/chat/completions` URL works via `OPENCODE_API_URL`.

The model writes **section headings and grouping, and nothing else.** It never
touches a headline or a blurb — those are quotations from the publisher, and a
paraphrase would leave the page looking like a feed reader while no longer
reporting what anyone actually said.

**A publish costs one request.** Every topic goes in a single batched call —
about 135 stories, well inside the context window — because it is the request
*count* that runs into a free-tier allowance, not the prompt size. Topics only
split across more than one request past `MAX_STORIES_PER_CALL` (220) in
`llm.py`, which exists to keep a reply from being truncated mid-JSON rather
than to respect any context limit.

With no key set — or when the endpoint rate-limits, errors, or returns
something that fails validation — sectioning falls back to a rule-based split
and those shards carry `"degraded": true`. That is a worse digest, not a
failed one. `run.py publish --dry-run` prints the headings and marks each
topic `[gemini-3.5-flash]` or `[rule-based]` so you can see which you got.

## Deploying

1. Move the workflow into place: `.github/workflows/digest.yml`.
2. Point GitHub Pages at the repository root (this directory *is* the site —
   `index.html` and `data/` sit at the top level).
3. Add the secret `OPENCODE_API_KEY` under Settings → Secrets and variables →
   Actions. `OPENCODE_API_URL` and `OPENCODE_MODEL` are optional and go in the
   **Variables** tab, not Secrets.

The workflow runs four times a day: three collect-only, and one at 13:00 UTC
that also publishes. The 6-hour interval is set by feed turnover, not
preference — `lobste.rs` and `r/programming` carry ~25 entries and turn over
about that fast, so a longer interval silently drops items.

## Layout

| Path | |
|---|---|
| `run.py` | CLI entry point |
| `sources.yaml` | source registry, topics, keyword routing, ranking knobs |
| `collect.py` | adapters (`feed` / `newsletter` / `hn`), conditional GET, the store |
| `models.py` | `Item`, `Cluster`, URL canonicalisation, title matching, scoring |
| `compose.py` | suppression, story arcs, sectioning, `feed.xml`, `health.json` |
| `llm.py` | optional section-heading call |
| `publish.py` | shard writer, manifest rebuild, retention prune |
| `index.html` | the reader |
| `health.html` | per-source status |
| `sw.js` | offline: shell cache-first, data network-first with a 2.5s timeout |
| `data/` | published shards, manifest, RSS feed — served over HTTPS |
| `store/` | durable JSONL, poll state, caches, already-published set — **not** served content |

Raw JSONL lives in `store/`, not `data/`, precisely because `data/` is served
over HTTPS and the raw store has no business being there.

Shards older than 120 days are pruned from `data/`, but their raw items stay
in `store/items/*.jsonl` — a longer window is a rebuild away, not a data loss.

## Reading it

- **Tally** — one stroke per independent source, struck through in fives.
- **Arcs** — a story that was published before and has since gained a source
  returns marked `day 2 · was 1 src`: the tally *moving*, rather than a single
  reading of it. Stories that have not gained a source do not come back.
- **Unread** — a red dot until you open it. Kept in `localStorage`, per
  device, never required for the page to work.
- **Topics** — pick any subset; each is a separate shard, so choosing 3 of 17
  does not download the other 14. Selection lives in the URL hash and is
  shareable.
- **Search** filters the shards you have loaded, client-side.

## Known gaps

Tracked in full in `CHANGES.md`, but the ones worth knowing up front:

- **A free tier still caps you per day.** Composition is one request per
  publish, so a once-daily schedule costs one request a day. But the allowance
  is a daily one: once it is spent, further requests 429 no matter how few you
  make, and every topic falls back to rule-based sections for that run. This
  is survivable by design — the digest publishes complete either way — but if
  a run degrades, the fix is to wait for the reset, not to retry.
- **Corroboration reads low on a cold start.** Only Hacker News can backfill
  (Algolia is a time-range query, so it reaches back 7 days on first run);
  Lobsters and Reddit expose ~25 current entries and nothing more. On day one
  only 7 of 141 published stories clustered across sources. It climbs as the
  store fills.
- **Suppression has no escape hatch.** Gaining a source is the only
  readmission rule, so a story everyone carried on day one and keeps
  discussing never returns.
- **No test suite.** `canonical_url` and `titles_match` decide whether the
  tally works at all, and are covered only by hand-checked examples in their
  docstrings.
- **Two documented sources are missing.** Python Weekly and tl;dr sec both
  moved to beehiiv and their old URLs now serve HTML. Add the correct
  `rss.beehiiv.com` feeds to restore them.
