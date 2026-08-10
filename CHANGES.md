# CHANGES.md

Design record for the multi-source rewrite of `tldr-summarizer-bot`.
Written 2026-08-09. Captures what changed from the original single-publisher
bot, **why**, and what is still outstanding.

---

## Goal

Generalise from TLDR-only to breadth: ~30 sources across ~20 topics, still
free, running on GitHub Actions alone, publishing to a static PWA on Pages.

## What the original design relied on (and lost)

TLDR gave three things for free. Every major change below exists because one
of them disappeared:

| Assumption | Replacement |
|---|---|
| Pre-curated: ~10 stories per topic per day | Ranking stage (score → cap → select) |
| One page per topic per date | Per-source cadence + polling state |
| Idempotency = "last issue date sent" | Per-item identity + `editions` keyed on (date, topic) |

## Scope decisions

- **Breadth over depth.** Considered a narrow CVE/dependency watcher or a
  preprint tracker instead; both need a different ranking model and neither
  benefits from clustering. Breadth chosen; the narrow variants remain viable
  as a second consumer of the same store.
- **No Cloudflare Worker.** Push-only. Drops `/digest`, `/news <topic>` and
  grounded Q&A. `workflow_dispatch` partially covers on-demand builds; the
  Q&A has no free static equivalent and is simply gone.
- **GitHub Actions only.** No always-on process, no external database.

---

## Architecture

```
sources.yaml
    │
    ▼
[poll]      per-source cadence, conditional GET (ETag / If-Modified-Since)
    │
[normalize] every adapter emits Item — nothing downstream knows what a feed is
    │
    ▼
data/items/YYYY-MM-DD.jsonl        ← the durable store (append-only, in repo)
    │
[rebuild]   in-memory SQLite from last 30 days, every run
    │
[cluster]   canonical URL, then SimHash on titles
    │
[rank]      arithmetic only — no LLM
    │
[compose]   one LLM call per (date, topic)
    │
    ├──► Telegram
    └──► docs/data/{date}/{topic}.json  +  docs/data/index.json
```

### Key decisions

**Ingestion is separated from composition even though one workflow run does
both.** The seam is what allows re-polling without re-sending, backfilling a
missed day, and moving to a different schedule later without a rewrite.
Collapse the *schedule*, not the code.

**Storage is JSONL in the repo, not a committed SQLite file.** Git stores a
full copy of a binary blob on every commit — tens of MB/month of undiffable
history. JSONL diffs as text, compresses, and is readable in a PR. SQLite is
rebuilt in memory each run (a few thousand rows, sub-second); `schema.sql` is
therefore a build artifact, never committed data.

**Ranking is deliberately arithmetic.** Its entire purpose is bounding token
cost as source count grows; an LLM at that stage defeats it. Score is
`recency_decay × source_weight × corroboration × log(engagement) × interest`,
then a per-source cap, then a fixed budget per topic.

**A repeated URL is a *sighting*, not a new row** (`item_sightings`). Cross-
source repetition is the strongest ranking signal breadth buys you — it is
what TLDR's human curation was providing implicitly.

**`canonical_url()` is load-bearing.** Two sources must produce byte-identical
URLs or clusters silently split into apparent duplicates. Handles: tracking
param stripping (with a `_SIGNIFICANT_PARAMS` allowlist so `?v=` survives),
host normalisation, Medium/Substack trailing hash slugs, and newsletter click-
wrapper unwrapping (`links.cooperpress.com` etc., resolved via HEAD, cached
permanently).

**Polling cadence is set by feed turnover, not preference.** A source is safe
if it publishes fewer items per interval than its feed carries. Newsletters
and eng blogs are safe daily; HN via Algolia is safe at any interval (time-
range API, queryable retroactively); `lobste.rs` and `r/programming` carry
~25 entries and turn over about that fast — they set the 6h floor.

**The manifest is rebuilt by directory scan, never mutated.** One failed run
must not leave a manifest that lies about what is on disk.

---

## Sources

26 configured in `sources.yaml`, grouped by role:

- **Curated newsletters** (weight 1.1–1.4) — the backbone. TLDR (4 topics,
  existing HTML fetcher retained) plus the **Cooper Press family**
  (JavaScript Weekly, Node Weekly, React Status, Frontend Focus, Golang
  Weekly, Postgres Weekly, DB Weekly): one publisher, one feed shape, seven
  topics from a single adapter — the cheapest breadth available.
  Also tl;dr sec, SRE Weekly, Changelog, Import AI.
- **Aggregators** (0.6–0.9) — Hacker News (Algolia, `min_points: 150`
  server-side), Lobsters, r/programming, InfoQ. Low weight and hard-capped;
  their value is corroboration, not individual items.
- **Company eng blogs** (1.0–1.1) — Cloudflare, Netflix, Stripe, Grab. Fanned
  into a shared `engineering` topic so no one company dominates a section.
- **Release feeds** (0.9–1.2) — Spring Blog, `spring-boot/releases.atom`, AWS.
  Kept on their own `releases` topic so actionable items never crowd out news.

---

## Files

| File | Status | Purpose |
|---|---|---|
| `sources.yaml` | written | Source registry + topic list + keyword routing + ranking knobs. Adding a source is a config edit, never code. |
| `models.py` | written | `Item`, `Cluster`, canonicalisation, SimHash dedup, scoring, selection. |
| `collect.py` | written | Adapters (`feed` / `newsletter` / `hn`), conditional GET, click-wrapper resolution, JSONL store. |
| `compose.py` | written | Suppress, rank, select, section, enrich blurbs, write `feed.xml` + `health.json`. |
| `llm.py` | written | One call per (date, topic) for section headings and grouping only. Falls back silently when no key is set. |
| `health.html` | written | Reader-facing source status, from `data/health.json`. |
| `icon-192.png`, `icon-512.png` | written | Rasterised from `icon.svg`; artwork sits inside the maskable safe zone. |
| `run.py` | written | CLI: `collect`, `publish`, `verify`, `health`. PEP-723 header, so `uv run run.py` needs no project file. Loads `.env` at startup. |
| `.env.example` | written | Template for `OPENCODE_*`. Copy to `.env`, which `.gitignore` excludes. |
| `publish.py` | written | Shard writer, manifest rebuild, retention prune. Unmodified; now driven by `compose.publish`. |
| `schema.sql` | dropped | The in-memory SQLite rebuild was never needed: clustering and ranking run over a list of `Item` in one pass, and no query is issued that justifies a schema. Revisit if suppression (Outstanding 4) needs an index. |
| `digest.yml` | written | 4 runs/day: 3 collect-only, 1 collect+publish. Belongs at `.github/workflows/digest.yml`. |
| `index.html` | written | Static PWA reader. |
| `sw.js` | written | Shell cache-first; data network-first w/ 2.5s timeout; precaches 4 recent days. |
| `manifest.webmanifest`, `icon.svg` | written | PWA install. |
| `data/**` | live | Published shards, manifest, `feed.xml`, `health.json`. Written by `run.py publish`. |
| `store/**` | live | Durable JSONL store, poll state, resolved-link and blurb caches, source health, already-published set. |

Note on layout: this directory is simultaneously the repo root and the
published site root, so the durable store is `store/`, not `data/items/` as
sketched above — `data/` is served over HTTPS and raw JSONL has no business
being there.

### Workflow specifics

- Mode derived from which cron fired (`0 13 * * *` → publish, else collect).
- `concurrency: {group: digest, cancel-in-progress: false}` — a cancelled
  *collect* run permanently loses whatever the firehoses rotated out; a
  cancelled publish is merely re-runnable.
- Push uses 3× `git pull --rebase --autostash` retry: the concurrency group
  serialises runs but not a human pushing to `main` mid-run.
- Source health written to `$GITHUB_STEP_SUMMARY` — with 30 sources something
  is always broken, and it must be visible rather than silently absent.
- Actions cron drifts 5–15 min and occasionally skips a slot; nothing
  downstream may assume exact intervals. Date-keyed editions handle this.

### Web app

- **Signature element: the corroboration tally.** One stroke per independent
  source, struck through in fives. Makes "six outlets ran this" legible as a
  shape before the headline is read — the one thing this system computes that
  no other reader surfaces.
- Palette: bone `#E7E9E4`, ink `#14181F`, pine `#2F5D50` (chips), signal red
  `#B33A1A` (tally + unread only). Type: Bricolage Grotesque / Newsreader /
  IBM Plex Mono.
- Per-topic shards: a reader picking 3 of 20 topics must not download 17.
- Filter state in the URL hash (shareable); localStorage only remembers topic
  choice, in try/catch, never required.
- Client-side search over loaded shards replaces `/news <topic>`. Plain
  `filter()` is adequate at a few thousand items; lunr.js only if the full
  backlog is ever indexed.

---

## Outstanding

~~1. **RSS adapter + collect loop**~~ — done. `collect.py`: conditional GET,
per-source cadence, three adapters, failure recorded per source rather than
failing the run. Sighting upsert is implicit — a repeat of `(source,
canonical)` is skipped on append rather than counted; a real counter is only
needed once suppression (4) lands.

~~2. **Verify feed URLs**~~ — done, as `run.py verify` rather than
`tools/validate_sources.py`. All 26 remaining sources fetch and parse. Two
named in this document were **dropped**: Python Weekly and tl;dr sec both
moved to beehiiv and their documented URLs now serve HTML. Restore them by
adding the correct `rss.beehiiv.com` feed.

~~3. **Suppression of already-shipped items**~~ — done, and it turned out to
be the difference between a daily digest and the same digest daily. A 7-day
rebuild window meant every morning republished most of the previous morning.
`store/shipped.json` keys on canonical URL; a story returns only when it has
gained a source, and comes back carrying its arc (see below). Measured:
day 1 published 122 stories, a simulated day 2 published 76, **overlap zero**.

~~4. **Story arcs**~~ — new, and the reason readmission is a feature rather
than a leak. A story carried by 1 source on Monday and 2 by Tuesday returns
labelled `day 2 · was 1 src`: the tally *moving*, which is the one thing this
system computes that a single reading of it cannot show.

~~5. **Near-duplicate detection**~~ — SimHash removed, not tuned. See
`models.titles_match` for the measurements that killed it.

~~6. **LLM composition**~~ — written (`llm.py`), scope deliberately narrowed
to section headings and grouping. It does not touch headlines or blurbs,
because those are quotations from the publisher and a paraphrase would leave
the page looking like a feed reader while no longer reporting what anyone
said. Verified against a live Gemini endpoint: headings come back specific
("Postgres performance and evolution", "Securing the npm ecosystem") rather
than the generic labels the prompt warns against.

The operational constraint is **rate limiting, not quality**.

One call per topic was the original design, straight from this document. It
does not survive a free tier: seventeen calls fired back to back exhausted the
allowance partway through, and a real run composed 10 of 17 topics before
falling back. So composition is now **batched — the whole edition in one
request**, ~135 stories, keyed by topic with indices scoped per topic.
Validation stayed per topic, which is what makes a partial reply cost only the
topics it omitted rather than the day.

The prompt-size worry that argued against batching did not materialise: 135
stories is a few thousand tokens against a context measured in hundreds of
thousands. `MAX_STORIES_PER_CALL` exists only to stop a very long reply being
truncated mid-JSON.

Also added, after watching it fail: 429 treated as retryable (it is the one
4xx that heals), the server's own `retryDelay` honoured, calls throttled to
one per 4s, and a circuit breaker so an exhausted quota stops the run rather
than spending 21 doomed requests to rediscover itself.

**A daily cap still bites.** With batching a publish costs one request, but
once the day's allowance is gone a single request 429s just as readily as
seventeen, and the whole edition degrades. Publishing complete-but-degraded is
the correct outcome; there is no retry that helps.

~~7. **`icon-192.png` / `icon-512.png`**~~ — done.

~~8. **Cross-topic duplicate rendering**~~ — new bug, found in published data:
18 of 121 stories were routed into 2+ topics and drew once per shard, so a
reader with three topics selected saw the same story three times under three
headings. Deduplicated in the reader, not the router — the routing is correct,
it is the rendering that was wrong.

~~9. **Source health page**~~ — done. `health.html` distinguishes three states,
not two: "ok, last seen four days ago" is the failure that actually loses you
a source, and it errors nowhere.

~~10. **Outbound feed**~~ — `data/feed.xml`, one item per story, corroboration
count in the description.

Still open:

1. **Telegram send** — dropped from the publish path. The static site and the
   RSS feed are the only consumers today.
2. **Tests.** `canonical_url` and `titles_match` decide whether the tally works
   at all and have no test file. The ten title pairs in the `titles_match`
   docstring were checked by hand and should be a golden file.
3. **Retire the duplicate parser.** With the Worker gone, `worker/src/tldr.ts`
   can be deleted and the README's "keep them in sync" caveat removed.
4. **Cold-start corroboration.** Only Hacker News can backfill (Algolia is a
   time-range query, so it reaches back 7 days on first run); Lobsters and
   Reddit expose ~25 current entries and nothing more. The tally reads low
   until several days have accumulated — day 1 clustered only 7 of 141
   published stories across sources.
5. **Suppression has no escape hatch for a genuinely big story.** Gaining a
   source is the only readmission rule. A story everyone carried on day one
   and keeps discussing never returns.

## Rejected

- **Cloudflare D1** — existed only to give the Worker read access to what
  Python wrote. Moot once the Worker was dropped.
- **Committed SQLite binary** — git history bloat, undiffable.
- **LLM-based ranking** — reintroduces the cost problem ranking exists to solve.
- **Embeddings for near-duplicate detection** — still rejected, but the stated
  reason was wrong. SimHash was *not* sufficient: measured against one day of
  real headlines it could not separate duplicates from unrelated stories at
  any threshold. Token-set matching replaced it and needs no model or vector
  store either, so the conclusion survives its premise.
- **LLM-written headlines and blurbs** — the original bot let the model
  rewrite both. Here it may not. Everything on the page except a section
  heading is a quotation from a publisher, and a paraphrased quotation that
  still looks like a feed entry misrepresents what was actually said.
