# MDL Texture Editor

[![CI](https://github.com/yonatankarp/mdl-texture-editor/actions/workflows/ci.yml/badge.svg)](https://github.com/yonatankarp/mdl-texture-editor/actions/workflows/ci.yml)

A local web tool for previewing and improving the textures of GameStudio A5 /
Quake-lineage `.MDL` models. The left pane is an editable view of the model's
skin; the right pane renders the textured 3D model in real time. Paint on the
skin in the browser, or edit it in your usual image editor, and the model
re-textures live so you can see how a change looks on the mesh as you make it.

![The editor: the model's skin PNG on the left, the live textured 3D render on the right](docs/screenshots/split-view.jpg)

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

![The same model with the wireframe overlay enabled](docs/screenshots/wireframe.jpg)

`Flip V` inverts the model's vertical texture mapping and remembers the choice
per model (see below).

The paint toolbar above the left pane has a color picker, a brush-size slider,
and `Undo` / `Redo`. Undo/redo are also bound to `Ctrl/Cmd+Z` and `Ctrl/Cmd+Y`
(`Cmd+Shift+Z` works too).

## Orientation

The decoder flips texture V by default, which is correct for the large majority
of models (all standing figures). A minority of flat props (papers, vases,
coins) use the opposite vertical convention and load upside-down. Click
`Flip V` on those to correct them. The choice is saved per model in
`orientation.json` (keyed by absolute path), so you set it once and it sticks
the next time you open that model.

`orientation.json` holds only the exceptions and is git-ignored, since the
paths are specific to your machine.

## Editing a texture

You can edit two ways, and both feed the same live preview and save path.

**Paint in the browser.** Pick a color and brush size and paint directly on the
left pane; strokes appear on the 3D model as you draw. `Undo`/`Redo` (buttons or
`Ctrl/Cmd+Z` / `Ctrl/Cmd+Y`) step through your strokes.

![Painting a stroke on the skin, mirrored live on the 3D model](docs/screenshots/painting.jpg)

**Edit in an external editor.** Loading a model automatically extracts its skin
to a working folder (`_edit/<model>/skin0.png`) and shows that folder's path in
the toolbar. `Reveal folder` opens it (macOS). Edit `skin0.png` in any image
editor and save; the tool watches the file and re-textures the model live.
(Reloading a model reuses an existing working skin, so it won't discard
unsaved edits.)

When it looks right, click `Save to .MDL` to re-embed the edited skin into the
binary model. The first extract backs up the untouched original to
`_backup_mdl/<model>`, and every save rebuilds from that backup, so repeated
saves never compound and the original is always recoverable. Skins are
re-embedded as RGB565 (8-bit models are upgraded on save).

The `_edit/` and `_backup_mdl/` folders are git-ignored.

## Layout

```
server.py          Flask backend: geometry, skin, orientation, extract/save, watch, file-pick
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
