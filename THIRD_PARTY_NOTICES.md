# Third-party software

Agent Library's own source is MIT-licensed. Bundled components keep their own
licenses; the distribution's combined SPDX expression does not relicense any
component. Full license texts accompany source archives and installed packages.

| Component | Bundled version | License | License file |
| --- | --- | --- | --- |
| markdown-it | 15.0.3 | MIT | `web/vendor/markdown-it.LICENSE` |
| DOMPurify | 3.4.17 | Apache-2.0 (selected from Apache-2.0 OR MPL-2.0) | `web/vendor/dompurify.LICENSE`; alternative text `dompurify.LICENSE-MPL` retained |
| highlight.js CDN assets | 11.12.0 | BSD-3-Clause | `web/vendor/highlight.LICENSE` |
| KaTeX JavaScript and CSS | 0.19.1 | MIT | `web/vendor/katex.LICENSE` |
| KaTeX mathematical fonts | font set in 0.19.1 | OFL-1.1 | `web/vendor/katex-fonts.LICENSE-OFL` |
| mdurl, inside markdown-it's browser bundle | 2.1.0 | MIT | `web/vendor/mdurl.LICENSE` |
| uc.micro, inside markdown-it's browser bundle | 3.0.0 | MIT | `web/vendor/uc.micro.LICENSE` |
| entities, inside markdown-it's browser bundle | 8.1.0 | BSD-2-Clause | `web/vendor/entities.LICENSE` |
| linkify-it, inside markdown-it's browser bundle | 6.1.0 | MIT | `web/vendor/linkify-it.LICENSE` |
| punycode.js, inside markdown-it's browser bundle | 2.3.1 | MIT | `web/vendor/punycode.js.LICENSE` |

Paths above are relative to `src/agent_library/`. Version, npm tarball integrity,
original member path, and SHA-256 for each bundled file are recorded in
`web/vendor/VERSIONS.json`, `web/vendor/katex.VERSION.json`, and
`web/vendor/LICENSES.json`. The last file records the embedded dependencies'
upstream source-map/lockfile evidence and the fonts' embedded copyright and
reserved-name records.

The unmodified fonts retain their OFL notices and reserved font names. Their
copyright holders are Design Science, Inc. (2009–2010) and Khan Academy
(2014–2018); they are not covered by the project's MIT license. The complete
OFL text and all reserved names are supplied with the fonts. Runtime files have
not been altered to add these notices.

Run `python scripts/verify_vendor.py` to check every bundled file against these
records. The reader uses these local files and does not request CDN resources.
To update a component, obtain its official npm package, check its integrity,
replace only the declared vendor files and license texts, update the provenance
records, and run the vendor and renderer checks. Development-only jsdom is
pinned in `package-lock.json` and is not bundled with the application.
