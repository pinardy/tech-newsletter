"""One LLM call per (date, topic): themed section headings and grouping.

Scope is deliberately narrow. The model decides *how the day is organised* —
how many sections, what they are called, which story goes in which and in what
order. It does not write, rewrite or reorder a single word of a headline or
blurb, and it never sees or returns a URL.

That is a smaller job than the original bot gave its model, and the reason is
that everything else on the page is a quotation. A headline is the
publisher's claim and a blurb is the publisher's own description; once a model
paraphrases them the page still looks like a feed reader but is no longer
reporting what anyone actually said. Section headings are the one part of this
page nobody is being quoted on, so they are the one part a model may write.

Configuration matches the existing bot: OPENCODE_API_KEY / OPENCODE_API_URL /
OPENCODE_MODEL, any OpenAI-compatible chat/completions endpoint, defaulting to
Gemini's. No key configured means `compose` returns None and the caller keeps
its rule-based sections and the `degraded` flag.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_API_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
DEFAULT_MODEL = "gemini-3.5-flash"

TIMEOUT = 120
ATTEMPTS = 3
RETRY_DELAY = 5

# 429 is a 4xx that *is* worth retrying, unlike the rest of them. Free tiers
# meter per minute, and a publish fires one call per topic back to back:
# seventeen topics against a 15 RPM allowance rate-limits the tail of the run
# every time. Observed doing exactly that — 8 topics composed, 9 fell back.
RETRYABLE = {408, 429, 500, 502, 503, 504}

# Floor on the gap between calls, for the same reason. A daily job does not
# care that it takes 70 seconds instead of 20, and staying under the limit
# beats retrying after breaching it.
MIN_INTERVAL = 4.0
_last_call = 0.0


def _throttle() -> None:
    global _last_call
    wait = MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _retry_after(body: str, attempt: int) -> float:
    """Honour the server's own backoff hint when it sends one.

    Gemini returns a RetryInfo detail like {"retryDelay": "26s"}; guessing
    shorter than that just burns another request against the same limit.
    """
    payload = _error_payload(body) or {}
    for detail in payload.get("error", {}).get("details", []):
        if not isinstance(detail, dict):
            continue
        raw = str(detail.get("retryDelay", ""))
        if raw.endswith("s") and raw[:-1].replace(".", "").isdigit():
            return min(float(raw[:-1]) + 1, 90.0)
    return min(RETRY_DELAY * (2 ** attempt), 60.0)

# One request covers every topic in the edition. Seventeen separate calls in
# quick succession is what exhausts a free-tier allowance partway through a
# publish; batching turns a whole day into one request, which no per-minute
# limit notices.
#
# Indices are scoped per topic, and the payload is deliberately terse ("t",
# "n") because the prompt now carries every story in the edition rather than
# a twelfth of them.
SYSTEM_PROMPT = """\
You are the section editor for a daily engineering news digest. You will be \
given the stories selected for SEVERAL topics, each already ranked, as JSON:

{"<topic id>": {"label": "...", "stories": [{"i": 0, "t": "<headline>", \
"n": <how many independent outlets carried it>}, ...]}, ...}

For EACH topic independently, group that topic's stories into 2-4 themed \
sections and order them for reading. Rules:

- Indices are per topic. Within a topic, use EVERY index exactly once. Never \
move a story between topics, and never drop, merge or invent one.
- Section headings are yours to write: 2-5 words, specific to what is \
actually in that section, sentence case, no trailing punctuation. Prefer \
"Postgres 19 and the planner rewrite" over "News" or "Other updates".
- Lead each topic with the section its reader would most want first. Heavily \
corroborated stories (high "n") generally belong near the top, but a single \
significant release can outrank them.
- Judge each topic on its own. Do not write summaries, do not restate \
headlines, do not comment.

Respond with ONLY a JSON object, no prose and no markdown fences. Include \
every topic id you were given:
{"topics": {"<topic id>": {"sections": [{"heading": "...", \
"items": [0, 3, 5]}]}, ...}}\
"""

# Ceiling on stories per request. Not a context limit — it is nowhere near
# one — but a hedge against a long day producing a reply long enough to be
# truncated mid-JSON, which would cost every topic in the batch. Topics are
# split across as few requests as this allows.
MAX_STORIES_PER_CALL = 220


class LLMError(Exception):
    pass


class QuotaExceeded(LLMError):
    """429 that survived its retries — the allowance is gone, not congested."""


# Once the quota is gone it is gone for the rest of the run. Without this, the
# remaining topics each burn three more requests and sleep through their
# backoffs to learn the same thing: a 17-topic publish spends 21 doomed calls
# and several minutes discovering it is still rate-limited.
_quota_exhausted = False


def _error_payload(text: str) -> dict | None:
    """Pull the error object out of a response body.

    Two shapes have to survive here. This endpoint returns the error wrapped
    in a JSON *array*, and the body is often truncated before it is parsed, so
    a plain `json.loads` fails on both counts. `raw_decode` reads the first
    complete value and ignores whatever follows it.
    """
    for i, ch in enumerate(text):
        if ch in "[{":
            try:
                payload, _ = json.JSONDecoder().raw_decode(text[i:])
            except ValueError:
                return None
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            return payload if isinstance(payload, dict) else None
    return None


def _summarise(err: Exception) -> str:
    """One readable line. The raw 429 body is 300 characters of JSON."""
    text = str(err)
    payload = _error_payload(text)
    message = (payload or {}).get("error", {}).get("message") or ""
    if message:
        prefix = text.split(":", 1)[0]
        return f"{prefix}: {message.split('.')[0]}"
    return text[:160]


def configured() -> tuple[str, str, str] | None:
    """(url, key, model) if an API key is present, else None."""
    key = os.environ.get("OPENCODE_API_KEY", "").strip()
    if not key:
        return None
    return (
        os.environ.get("OPENCODE_API_URL") or DEFAULT_API_URL,
        key,
        os.environ.get("OPENCODE_MODEL") or DEFAULT_MODEL,
    )


def _post(url: str, key: str, body: dict) -> dict:
    """POST with one retry on transient failure. 4xx is not retried."""
    payload = json.dumps(body).encode("utf-8")
    last: Exception | None = None

    for attempt in range(ATTEMPTS):
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })
        delay = RETRY_DELAY
        try:
            _throttle()
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            # Keep the body whole. Truncating it here is what stops
            # `_summarise` from parsing the error and reducing it to one line:
            # a pretty-printed 429 does not reach its "message" field inside
            # the first 200 characters, so the log gets raw JSON instead.
            last = LLMError(f"HTTP {e.code}: {body}")
            if e.code not in RETRYABLE:
                break              # a bad request will not heal on retry
            delay = _retry_after(body, attempt)
        except Exception as e:     # noqa: BLE001 — timeouts, connection resets
            last = e
        if attempt < ATTEMPTS - 1:
            time.sleep(delay)

    if isinstance(last, LLMError) and "HTTP 429" in str(last):
        raise QuotaExceeded(str(last))
    raise LLMError(str(last))


def _extract_json(content: str) -> dict:
    """Models fence JSON despite being asked not to. Tolerate it."""
    content = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", content, re.S)
    if fenced:
        content = fenced.group(1)
    return json.loads(content)


def _chunk(batch: dict) -> list[list[str]]:
    """Split topics into as few requests as MAX_STORIES_PER_CALL allows."""
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for topic, (_, clusters) in batch.items():
        n = len(clusters)
        if current and size + n > MAX_STORIES_PER_CALL:
            chunks.append(current)
            current, size = [], 0
        current.append(topic)
        size += n
    if current:
        chunks.append(current)
    return chunks


def compose_all(batch: dict) -> dict[str, list[tuple[str, list[int]]]]:
    """{topic: (label, clusters)} -> {topic: [(heading, [indices])]}.

    Topics absent from the result get rule-based sections. A missing topic is
    never fatal and never silent: partial success is the normal outcome when a
    reply covers most of the batch, and the caller marks whatever it did not
    get as `degraded`.
    """
    global _quota_exhausted
    settings = configured()
    if not settings or not batch or _quota_exhausted:
        return {}
    url, key, model = settings

    out: dict[str, list[tuple[str, list[int]]]] = {}
    chunks = _chunk(batch)
    total = sum(len(c) for _, c in batch.values())
    print(f"  llm: composing {len(batch)} topics / {total} stories "
          f"in {len(chunks)} request{'s' if len(chunks) != 1 else ''}")

    for chunk in chunks:
        payload = {
            topic: {
                "label": batch[topic][0],
                "stories": [
                    {"i": i, "t": c.representative.title, "n": len(c.sources)}
                    for i, c in enumerate(batch[topic][1])
                ],
            }
            for topic in chunk
        }

        try:
            resp = _post(url, key, {
                "model": model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content":
                        json.dumps(payload, ensure_ascii=False)},
                ],
            })
            content = resp["choices"][0]["message"]["content"]
            topics = _extract_json(content).get("topics") or {}
        except QuotaExceeded as e:
            _quota_exhausted = True
            print(f"  llm: quota exhausted — remaining topics will use "
                  f"rule-based sections ({_summarise(e)})")
            break
        except (LLMError, KeyError, IndexError, ValueError, TypeError) as e:
            print(f"  llm: request failed, those topics fall back "
                  f"({_summarise(e)})")
            continue

        if not isinstance(topics, dict):
            print("  llm: reply was not keyed by topic; falling back")
            continue

        for topic in chunk:
            section_spec = topics.get(topic)
            if not isinstance(section_spec, dict):
                continue
            validated = _validate(section_spec.get("sections"),
                                  len(batch[topic][1]), topic)
            if validated:
                out[topic] = validated

    missed = [t for t in batch if t not in out]
    if missed:
        print(f"  llm: {len(missed)} topic(s) fell back to rule-based: "
              f"{', '.join(sorted(missed))}")
    return out


def _validate(sections, n: int, topic: str) -> list[tuple[str, list[int]]] | None:
    """Enforce the contract the prompt asked for.

    A model that drops three stories produces a page that silently omits them,
    which is indistinguishable from a ranking decision. So: every index must
    appear exactly once. Duplicates are dropped, stragglers are appended to a
    final section rather than lost, and anything structurally wrong falls back.
    """
    if not isinstance(sections, list) or not sections:
        return None

    out: list[tuple[str, list[int]]] = []
    seen: set[int] = set()
    for section in sections:
        if not isinstance(section, dict):
            return None
        heading = str(section.get("heading", "")).strip().rstrip(".").strip()
        if not heading or len(heading) > 60:
            return None
        picks = []
        for idx in section.get("items") or []:
            if not isinstance(idx, int) or not 0 <= idx < n or idx in seen:
                continue           # hallucinated or repeated index
            seen.add(idx)
            picks.append(idx)
        if picks:
            out.append((heading, picks))

    if not out:
        return None

    missing = [i for i in range(n) if i not in seen]
    if missing:
        print(f"  llm: {topic} left {len(missing)} stories unassigned; appending")
        out.append(("Also circulating", missing))
    return out
