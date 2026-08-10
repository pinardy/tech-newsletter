# Fonts

Self-hosted rather than loaded from Google Fonts, so that first paint never
waits on a third party and the offline shell renders in its own type.

Each file is the Google Fonts `latin` or `latin-ext` subset, further reduced
with `fonttools` to the weights this site actually sets:

| Family | Files | Kept |
|---|---|---|
| Bricolage Grotesque | `bricolage-*` | variable `opsz` 12–96, `wght` 600–800 |
| Newsreader | `newsreader-*` | variable `wght` 400–500, `opsz` pinned to 16 |
| Newsreader Italic | `newsreader-italic-*` | static 400, `opsz` pinned to 16 |
| IBM Plex Mono | `plexmono-400-*`, `plexmono-500-*` | static 400 and 500 |

`latin` is precached by the service worker; `latin-ext` is fetched only when a
headline needs it, then cached at runtime.

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
