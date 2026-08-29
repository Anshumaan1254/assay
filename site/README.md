# site/

The standalone landing page. A scroll story for the project, deployed to
Vercel as a static SPA.

Not to be confused with [`reviewer/`](../reviewer), which is a live
read-only UI over a computed audit and needs the FastAPI app behind it. This
page is static, has no API, and links through to the reviewer's source and
the one command that starts it. **Nothing in `reviewer/` changes for this
page to exist**, and `tests/test_site.py` asserts the site never imports it.

```
make site         # dev server, hot reload
make site-build   # production build into dist/
make site-data    # re-bake src/data/audit.json from a signed report
make site-art     # regenerate the parallax layers
```

## The scroll

| # | Section | What it does |
|---|---|---|
| 1 | `scroll-locked-video-hero` | Locks the body and drives the video off wheel input, then hands the page back at the end |
| 2 | `LineStory` | The argument, one line at a time — each line focuses in, the previous one blurs out |
| 3 | `hero-scroll-video-pin-reveal` | Pinned clip-path circle widening from a point, with the kinetic headline and tag badges |
| 4 | `parallax-scrolling` | The ticker wall, four layers at four speeds |
| 5 | `Closing` | Run it, and the way through to the reviewer |

`recursive-erosion` renders behind all of it as a fixed background.

## Changes made to the supplied components

They were pasted in as-is apart from copy, palette, and four integration
fixes that the page does not work without:

- **The hero had no way out.** As supplied it pinned `document.body` and
  only released on unmount, so nothing below it was reachable. It now
  releases on forward input at the end of the video (which is what its own
  header comment describes), re-locks only at the very top of the document,
  never locks at all under `prefers-reduced-motion`, and releases on
  <kbd>Esc</kbd>/<kbd>Tab</kbd>/<kbd>PageDown</kbd> so keyboard users are
  not trapped.
- **Two components each built their own Lenis.** Two instances hijacking the
  wheel and both driving `ScrollTrigger.update` is not smoother scrolling —
  it is two objects disagreeing about the scroll position. They share the
  refcounted singleton in `src/lib/smooth-scroll.ts`; a test asserts nothing
  else constructs one.
- **Two components killed every ScrollTrigger on the page** in cleanup, not
  just their own. Scoped to their own triggers.
- **`recursive-erosion-utils/recursive-erosion-source` did not exist.** The
  component imports it and it was not part of what was supplied, so it is
  authored here: a dependency-free 2D-canvas particle sphere, seeded, in the
  reviewer's palette. Deliberately not a CDN three.js build — the iframe is
  `sandbox="allow-scripts"` with an opaque origin, and a background that
  goes blank offline is worse than no background.

`src/main.tsx` also does not use `<StrictMode>`; its comment explains why.

## The numbers

Every figure comes from `src/data/audit.json`, baked by
[`scripts/bake_site_data.py`](../scripts/bake_site_data.py) from a signed
`report.json` and the committed eval sweep. Each amount arrives as the exact
integer paise the engine computed *and* the string to print, so the browser
never divides by 100 and never sums a column —
[`tests/test_site.py`](../tests/test_site.py) enforces that, along with
invariant 3 holding over the baked numbers and invariant 5 (nothing here may
reach `datagen/`).

`runs/realistic-seed42/report.json` is derived and gitignored, so
`src/data/audit.json` is the committed truth. Run `make demo` before
`make site-data` if the report is not there.

## Parallax art

`scripts/gen_parallax_layers.py` draws the four layers into
`public/parallax/`. They are transparent SVG chart art over the ground, at
four densities and four speeds — near objects bigger, sparser and sharper
than far ones, which is the only reason parallax reads as depth.

To use a real screenshot instead, drop it at
`public/parallax/custom-layer-2.png`. The mid layer prefers that file and
falls back to the generated SVG when it is absent — no code change.

## Deploying to Vercel

Import the repo and set **Root Directory** to `site`. `vercel.json` supplies
the rest (Vite preset, `npm ci`, `dist`, asset caching). The build needs no
Python, no API key and no network beyond npm — the numbers are already in
the repo.

`SITE_BASE` overrides the base path if this is ever served from a
subdirectory instead (`SITE_BASE=/assay/ npm run build`).

## Design rules

Reviewer palette throughout: `#08090b` ground, four greys, and `#e8b44a`
used for exactly one thing — money that could not be explained. Two
typefaces: Inter for display, JetBrains Mono for every number, always
tabular.
