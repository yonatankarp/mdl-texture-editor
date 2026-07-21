# MDL Texture Editor

A local web tool for previewing and improving the textures of GameStudio A5 /
Quake-lineage `.MDL` models. The left pane shows the model's skin as a flat
PNG; the right pane renders the textured 3D model in real time. Edit the skin
in your usual image editor and the model re-textures live, so you can see how a
change looks on the mesh as you make it.

It reads three model formats: **IDPO** (Quake1-style), **MDL5**, and **MDL3**
(both A5). MDL4 and MDL2 are not supported.

## Requirements

Python 3.9+ and the packages in `requirements.txt` (Flask and Pillow). The
native file picker (the Browse button) is macOS-only; on other platforms type
an absolute path into the field instead.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
python server.py
```

Then open http://127.0.0.1:5005. It binds to localhost only and is meant as a
single-user tool, so it opens models anywhere on disk without a sandbox.

The field accepts either a path relative to this folder (for example
`samples/Paper2.MDL`) or an absolute path to any model on your machine. A few
sample models ship in `samples/`.

## Controls

`Browse` opens a native file dialog (macOS). `Load` loads the path in the field.

`565 preview` toggles a CPU RGB565 quantization of the skin, matching how the
A5 engine stores most textures, so you can preview the in-game color depth
instead of the full-color PNG.

`Wireframe` overlays the mesh edges.

`Flip V` inverts the model's vertical texture mapping and remembers the choice
per model (see below).

## Orientation

The decoder flips texture V by default, which is correct for the large majority
of models (all standing figures). A minority of flat props (papers, vases,
coins) use the opposite vertical convention and load upside-down. Click
`Flip V` on those to correct them. The choice is saved per model in
`orientation.json` (keyed by absolute path), so you set it once and it sticks
the next time you open that model.

`orientation.json` holds only the exceptions and is git-ignored, since the
paths are specific to your machine.

## External-editor hot-reload

The viewer watches a skin PNG on disk and re-textures the model whenever that
file changes, so you can paint in any editor and watch the result update.

First extract a model's skin to a PNG:

```bash
python mdl_tool.py extract samples/Paper2.MDL samples/_skins/Paper2
```

Then edit `samples/_skins/Paper2/skin0.png` in your image editor and save. The
watched path is currently hardcoded near the top of `static/app.js`
(`WATCH_PNG`); point it at your extracted skin if you work on a different model.

`mdl_tool.py` also imports an edited skin back into the binary model
(`python mdl_tool.py import ...`), which is how a finished texture gets written
back to the `.MDL`.

## Layout

```
server.py          Flask backend: geometry, skin, orientation, watch, file-pick
mdl_geometry.py    Pure-Python MDL geometry decoder (IDPO / MDL5 / MDL3)
mdl_tool.py        Skin decode/encode + extract/import CLI (8-bit and 565)
game_palette.raw   256-color palette for 8-bit skins
static/            Three.js viewer (index.html, app.js, vendored three.js)
samples/           A few small models for a first run and for the tests
tests/             pytest suite for the decoder and the server
```

## Tests

```bash
python -m pytest
```

## Origin

Extracted from the Piposh 3D Remaster project as a reusable, self-contained
tool. `mdl_tool.py` and `game_palette.raw` are vendored from that project so
this repo has no external dependency on it.
