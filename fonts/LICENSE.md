# Fonts

Self-hosted rather than loaded from Google Fonts, so that first paint never
waits on a third party and the offline shell renders in its own type.

Each file is the Google Fonts `latin` or `latin-ext` subset, further reduced
with `fonttools` to what this site actually sets:

| Family | Files | Kept |
|---|---|---|
| Bricolage Grotesque | `bricolage-600-latin`, `bricolage-800-latin` | static instances at `opsz` 18 and 48 |
| Bricolage Grotesque | `bricolage-latin-ext` | variable `opsz` 12–96, `wght` 600–800 |
| Newsreader | `newsreader-latin*` | variable `wght` 400–500, `opsz` pinned to 16 |
| Newsreader Italic | `newsreader-italic-latin` | static 400, `opsz` 16, ASCII + typographic punctuation |
| IBM Plex Mono | `plexmono-400-*`, `plexmono-500-*` | static 400 and 500 |

`latin` is precached by the service worker; `latin-ext` is fetched only when a
headline needs it, then cached at runtime.

Two of those rows are narrower than they look, on purpose:

* **Bricolage** is set at exactly two sizes — 600 for headings at 16–19px and
  800 for the wordmark at 30–68px — so the `latin` faces are static instances at
  those points rather than one variable file carrying an `opsz` range nobody
  asks for. That is 24KB off the critical path. `latin-ext` stays variable
  because it is only ever fetched for an accented headline, and one file there
  is simpler than two.
* **The italic** sets one hand-authored line per page (`.standfirst`) and
  nothing else, so it carries ASCII and English punctuation instead of all of
  Latin-1 — 24KB down to 13KB. Its declared `unicode-range` is narrowed to
  match, so an accented character in a standfirst falls cleanly to Georgia
  italic instead of selecting a face with no glyph for it. If you ever write a
  standfirst with an accent in it, widen both together.

## Licences

All three families are licensed under the SIL Open Font License, Version 1.1
(<https://scripts.sil.org/OFL>).

- **Bricolage Grotesque** — Copyright 2022 The Bricolage Grotesque Project
  Authors (<https://github.com/ateliertriay/bricolage>)
- **Newsreader** — Copyright 2020 The Newsreader Project Authors
  (<https://github.com/productiontype/Newsreader>)
- **IBM Plex Mono** — Copyright 2017 IBM Corp. (<https://github.com/IBM/plex>)

The OFL permits redistribution provided the licence travels with the fonts,
which is what this file is for. The reduction above is subsetting and axis
instancing only — no outline was altered — which is the same treatment Google
Fonts applies when it serves these families, and the family names are unchanged.
