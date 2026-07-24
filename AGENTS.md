# MDL Texture Editor — agent guide

Local web tool for previewing and editing textures (and nudging vertices) of
GameStudio A5 / Quake-lineage `.MDL` models. Flask backend, Three.js frontend.
Supported formats: IDPO (Quake1-style), MDL5, MDL3. Not supported: MDL2, MDL4.
See `README.md` for the full user-facing feature tour.

## Layout

```
server.py          Flask backend: geometry/skin APIs, extract/save, file watch, orientation
mdl_geometry.py    Pure-Python MDL geometry decoder (IDPO / MDL5 / MDL3)
mdl_tool.py        Skin decode/encode + extract/import CLI (8-bit and RGB565)
mdl_paper.py       Image -> flat "paper" IDPO model generator
game_palette.raw   256-color palette for 8-bit skins
static/            Frontend: index.html, app.js (paint/undo), meshedit.js (vertex gizmo)
static/vendor/     Vendored three.js + OrbitControls + TransformControls (no npm)
samples/           Small models for first runs and tests (Bad2.MDL has 7 skins)
tests/             pytest: decoder, server API, and Playwright frontend suites
```

Flat Python layout; `pyproject.toml` puts the repo root on `sys.path` so tests
`import server` directly. No package install step.

## Setup, run, test

Use the Makefile (`make help` lists targets):

- `make install-dev` — venv + all dependencies
- `make run` — starts the editor on **http://127.0.0.1:5005** (not 5000; on
  macOS port 5000 is taken by AirPlay and returns 403)
- `make test` — backend suites, skips browser tests (what the CI matrix runs)
- `make test-frontend` — Playwright suite; run `make browsers` once first
- `make test-all` — everything

The server runs with `debug=True`, so the Werkzeug reloader picks up Python
edits. **Don't kill/restart the server between verification runs** — leave it
running and let the reloader handle changes; static assets are re-read per
request anyway.

Frontend tests are marked `frontend` (see `pyproject.toml`) and are asserted
through user-visible state (canvas pixels, button disabled state), never
app.js internals. CI (`.github/workflows/ci.yml`) runs the backend suite on a
Python 3.11–3.13 matrix and the frontend suite once; branch protection
requires the aggregate `all-tests` check.

## Working-state directories (git-ignored)

- `_backup_mdl/` — pristine copy of every model made on first extract. Saves
  always rebuild from this backup so edits never compound. Never modify.
- `_edit/<model>/` — extracted working skins (`skin0.png` …) plus
  `vertices.json` (per-vertex deltas from wireframe nudging). The server
  watches these PNGs for external-editor hot reload.
- `orientation.json` — per-model Flip V overrides, keyed by absolute path
  (machine-specific, hence ignored).

## Conventions and gotchas

- Skins are re-embedded as RGB565 on save; 8-bit models are upgraded.
- Vertex nudges snap to the format's quantization grid, apply to all animation
  frames, and are clamped to the model's bounding box. Models with grouped
  IDPO frames are view-only.
- The Browse button and `Reveal folder` shell out to macOS (`osascript`,
  `open`); other platforms use typed paths.
- Binds to localhost only and opens files anywhere on disk by design — it's a
  single-user local tool, not a deployable service.
- `.claude/plans/`, `.claude/specs/`, and `docs/superpowers/` are brainstorm
  artifacts: leave them untracked, commit only source.
