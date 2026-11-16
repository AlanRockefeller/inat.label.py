# Local label-page dependencies

These files are served from the label generator's own static directory. No CDN
connection is needed for page JavaScript, stylesheets or webfonts.

- jQuery 3.6.0: unchanged version and bytes from the previous script URL.
  Its SHA-256 matches the integrity attribute already present in index.html.
  License: licenses/jquery-LICENSE.txt (MIT).
- Archivo, IBM Plex Mono, Newsreader: font files selected by the previous Google
  Fonts CSS query, including its weights, italics, optical sizing and unicode
  subsets. Font declarations and font-display: swap are preserved; src URLs
  now point to local files. Licenses are in licenses/*-OFL.txt (SIL OFL).

Pinned source URLs and SHA-256 hashes are in ASSET_MANIFEST.json. Update the
local files deliberately; do not silently replace these with moving CDN URLs.
External observation links and iNaturalist data API requests are separate from
these static browser dependencies.
