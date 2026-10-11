# Exculpata landing page

A static introduction to Exculpata, using the existing brand and application dark
theme. There is no build step, JavaScript, analytics, CDN, external font service,
or case material. All page resources are served from this directory. GitHub links
are ordinary navigation links, not automatic resource requests.

## Preview and publish configuration

From the repository root:

```console
python3 -m http.server 8080 --bind 127.0.0.1 --directory site
```

Open the loopback server in a browser. For a future Cloudflare Pages project,
select no framework, leave the build command empty, and set the build output
directory to `site`. The directory is the complete static output; it does not
need the Python application. `_headers` defines the Cloudflare response headers.
The HTML also includes a resource CSP for local preview; the Python preview
server does not apply `_headers`. No deployment or domain configuration is part
of this change. Leave Cloudflare Web Analytics and automatic script injection off
to preserve the page's no-tracking contract.

## Content and asset provenance

Claims were checked against the source at
`1a33ea829b7fe81514f247108a5c5d40f62ff6ae` (theme PR #174):

| Page content | Source |
| --- | --- |
| Positioning, tagline, local-first wording | `docs/BRAND.md`, `docs/BRANDING.md` |
| File types, source-linked questions, notes and report drafting | `README.md`, “What you can explore today” |
| Defense teams and access to counsel | `docs/PRODUCT_NORTH_STAR.md` |
| Alpha status, human review and synthetic evaluation | `docs/BRANDING.md`, `docs/PUBLIC_ALPHA.md`, `README.md` |
| Free, open-source core and license | `docs/PRODUCT_NORTH_STAR.md`, `LICENSE` |

The page does not promise on-prem-only operation, exhaustive review, guaranteed
accuracy, production acceptance, or planned frontier-agent capabilities.

`assets/exculpata-wordmark.svg` and `assets/exculpata-icon.svg` are unchanged copies
from `docs/assets/brand/`. The two variable WOFF2 fonts and their SIL Open Font
License notices are unchanged copies from `src/case_intelligence/static/fonts/`.
Colors track the dark values in `src/case_intelligence/static/theme-tokens.css`.
Copying these small assets keeps the output independently hostable. When updating
the brand, synchronize them with their canonical sources; the site tests check
byte equality.

## Verification

```console
.venv/bin/python -m pytest -q tests/test_static_site.py
```

These tests run in the ordinary application suite and check resource boundaries,
local paths, fragment targets, GitHub destinations, source asset fidelity, font
notices, and the page's script-free security policy. Before publishing, also run
the repository gates and installed pre-push publication check.

With Node.js, Playwright, and a Chromium executable available, run the standalone
browser regression from the repository root (the same dependency arrangement as
the other `scripts/browser-accept-*.cjs` checks):

```console
node scripts/browser-accept-static-site.cjs /path/to/chromium /tmp/fresh-site-check
```

It starts a disposable loopback server, blocks external requests, checks five
desktop/mobile viewports, and saves screenshots plus a receipt. The header must
contain only the brand/home link and GitHub link; both content sections remain
readable by scrolling. It also checks horizontal overflow, keyboard access,
local-only resources, and visible breathing room beneath the footer at the end
of the page. Desktop/mobile bottom-view screenshots complement the full-page
captures. Footer padding includes the mobile safe-area inset. The page has no
section-navigation controls or client JavaScript.

For browser acceptance, inspect desktop and mobile pixels, keyboard focus and
the skip link, all navigation destinations, and the network log. Confirm every
initial request stays on the preview origin and that no content overflows at
320px, 390px, tablet widths, and 200% desktop zoom. Keep screenshots and reports
outside the published `site/` directory.
