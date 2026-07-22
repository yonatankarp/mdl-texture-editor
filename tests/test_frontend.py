"""Browser-driven tests for the painting/undo/persist logic in static/app.js.

These drive the real page in headless Chromium (pytest-playwright). The paint
history (undoStack/redoStack/MAX_HISTORY) is module-local, so everything here is
asserted the way a user observes it: through the #undo/#redo disabled state and
the actual #paint canvas pixels, never by reaching into app.js internals.

Marked `frontend` so the plain `pytest` run (and the Python CI matrix) can skip
them with `-m "not frontend"`; a dedicated CI job installs the browser and runs
`-m frontend`.
"""
import pytest

pytestmark = pytest.mark.frontend

BRUSH_RGB = [255, 45, 85]  # #ff2d55, the default color input value


def open_editor(page, base_url):
    # Block the file-watcher SSE so it can never reload the canvas mid-test and
    # reset undo history; painting persistence is asserted separately. Must be
    # routed before goto so the initial subscribeWatch() is covered too.
    page.route("**/api/watch*", lambda route: route.abort())
    page.goto(base_url)
    # #reset is enabled only once extract sets editSkin, i.e. painting is armed.
    page.wait_for_selector("#reset:not([disabled])")


def open_editor_with_model(page, base_url, model):
    page.route("**/api/watch*", lambda route: route.abort())
    page.goto(base_url)
    page.locator("#path").fill(f"samples/{model}")
    page.locator("#load").click()
    page.wait_for_selector("#reset:not([disabled])")


def paint_dims(page):
    return page.evaluate(
        "() => { const p = document.getElementById('paint'); return [p.width, p.height]; }"
    )


def set_brush_size(page, size):
    # Range inputs ignore fill(); set the value and fire input so app.js updates.
    page.evaluate(
        "(s) => { const b = document.getElementById('brushsize');"
        " b.value = String(s); b.dispatchEvent(new Event('input')); }",
        size,
    )


def pixel(page, cx, cy):
    # RGBA of a single canvas pixel in canvas (intrinsic) coordinates.
    return page.evaluate(
        "([x, y]) => { const d = document.getElementById('paint')"
        ".getContext('2d').getImageData(x, y, 1, 1).data;"
        " return [d[0], d[1], d[2], d[3]]; }",
        [cx, cy],
    )


def stroke_at(page, cx, cy):
    # Paint a short stroke whose brush arc lands on canvas pixel (cx, cy).
    # canvasXY() in app.js maps client coords back to canvas pixels via the
    # element's rect, so we invert that mapping through the bounding box.
    canvas = page.locator("#paint")
    box = canvas.bounding_box()
    w, h = paint_dims(page)
    client_x = box["x"] + cx / w * box["width"]
    client_y = box["y"] + cy / h * box["height"]
    page.mouse.move(client_x, client_y)
    page.mouse.down()
    page.mouse.move(client_x + 1, client_y + 1)  # exercise the pointermove path
    page.mouse.up()


def test_stroke_then_undo_redo(page, live_server):
    open_editor(page, live_server)
    w, h = paint_dims(page)
    cx, cy = w // 2, h // 2

    original = pixel(page, cx, cy)
    assert original[:3] != BRUSH_RGB, "sanity: center must not already be brush color"

    stroke_at(page, cx, cy)
    assert pixel(page, cx, cy)[:3] == BRUSH_RGB
    assert page.locator("#undo").is_enabled()
    assert page.locator("#redo").is_disabled()

    page.locator("#undo").click()
    assert pixel(page, cx, cy) == original
    assert page.locator("#redo").is_enabled()

    page.locator("#redo").click()
    assert pixel(page, cx, cy)[:3] == BRUSH_RGB


def test_history_is_bounded(page, live_server):
    # MAX_HISTORY = 30. After 31 strokes the oldest snapshot (blank canvas,
    # pushed before stroke 1) is dropped, so at most 30 undos are possible and
    # the first stroke can never be undone away.
    open_editor(page, live_server)
    set_brush_size(page, 2)  # tiny arcs so the 31 strokes don't overlap
    w, h = paint_dims(page)
    y = h // 2
    n = 31
    step = w / (n + 1)
    xs = [round(step * (i + 1)) for i in range(n)]

    first_original = pixel(page, xs[0], y)
    last_original = pixel(page, xs[-1], y)
    for x in xs:
        stroke_at(page, x, y)
    assert pixel(page, xs[0], y)[:3] == BRUSH_RGB
    assert pixel(page, xs[-1], y)[:3] == BRUSH_RGB

    undos = 0
    while page.locator("#undo").is_enabled():
        page.locator("#undo").click()
        undos += 1
        assert undos <= n, "undo should have bottomed out at the history cap"

    assert undos == 30, f"expected 30 undos with MAX_HISTORY=30, got {undos}"
    # Fully unwound: the last stroke is gone, but the first stroke survives
    # because its pre-state fell off the bounded history.
    assert pixel(page, xs[-1], y) == last_original
    assert pixel(page, xs[0], y)[:3] == BRUSH_RGB
    assert first_original[:3] != BRUSH_RGB


def test_stroke_persists_to_working_skin(page, live_server):
    # Every committed edit POSTs the canvas to /api/skin-write. Assert the
    # frontend fires it with the working-skin file and a PNG data URL, both on
    # painting and on undo (afterEdit -> persistSkin).
    open_editor(page, live_server)
    w, h = paint_dims(page)
    cx, cy = w // 2, h // 2

    with page.expect_request("**/api/skin-write") as info:
        stroke_at(page, cx, cy)
    body = info.value.post_data_json
    assert body["file"].replace("\\", "/").startswith("_edit/")
    assert body["file"].endswith(".png")
    assert body["png"].startswith("data:image/png")

    with page.expect_request("**/api/skin-write") as undo_info:
        page.locator("#undo").click()
    assert undo_info.value.post_data_json["png"].startswith("data:image/png")


def test_reset_keeps_selected_skin(page, live_server_factory):
    # Regression coverage for multi-skin reset: force-extract returns skin0 as
    # `skin`, but the UI must keep editing the selected skin.
    open_editor_with_model(page, live_server_factory("Paper2.MDL", "Bad2.MDL"), "Bad2.MDL")
    page.wait_for_function("document.getElementById('skinselect').options.length === 7")
    page.wait_for_function(
        "() => { const p = document.getElementById('paint');"
        " return p.width === 640 && p.height === 400; }"
    )

    page.evaluate(
        "() => { const p = document.getElementById('paint');"
        " window.__skin0 = p.getContext('2d').getImageData(0, 0, p.width, p.height); }"
    )
    page.locator("#skinselect").select_option("1")
    page.wait_for_function(
        "() => {"
        " const p = document.getElementById('paint');"
        " const d = p.getContext('2d').getImageData(0, 0, p.width, p.height).data;"
        " const s0 = window.__skin0 && window.__skin0.data;"
        " if (!s0 || s0.length !== d.length) return false;"
        " for (let i = 0; i < d.length; i += 4) {"
        "   if (d[i] !== s0[i] || d[i + 1] !== s0[i + 1] || d[i + 2] !== s0[i + 2]) return true;"
        " }"
        " return false;"
        "}"
    )
    diff = page.evaluate(
        "() => {"
        " const p = document.getElementById('paint');"
        " const d = p.getContext('2d').getImageData(0, 0, p.width, p.height).data;"
        " const s0 = window.__skin0.data;"
        " for (let i = 0; i < d.length; i += 4) {"
        "   if (d[i] !== s0[i] || d[i + 1] !== s0[i + 1] || d[i + 2] !== s0[i + 2]) {"
        "     return { x: (i / 4) % p.width, y: Math.floor((i / 4) / p.width),"
        "       skin0: [s0[i], s0[i + 1], s0[i + 2], s0[i + 3]],"
        "       skin1: [d[i], d[i + 1], d[i + 2], d[i + 3]] };"
        "   }"
        " }"
        " return null;"
        "}"
    )
    assert diff, "Bad2 skin 1 should differ from skin 0 somewhere"
    assert diff["skin0"] != diff["skin1"]

    set_brush_size(page, 8)
    stroke_at(page, diff["x"], diff["y"])
    assert pixel(page, diff["x"], diff["y"])[:3] == BRUSH_RGB

    page.locator("#reset").click()
    page.locator("#reset", has_text="Confirm reset?").click()
    page.wait_for_function(
        "([x, y, expected]) => {"
        " const d = document.getElementById('paint').getContext('2d')"
        "   .getImageData(x, y, 1, 1).data;"
        " return d[0] === expected[0] && d[1] === expected[1] && d[2] === expected[2];"
        "}",
        arg=[diff["x"], diff["y"], diff["skin1"]],
    )

    assert page.locator("#skinselect").input_value() == "1"
    assert pixel(page, diff["x"], diff["y"]) == diff["skin1"]


def test_animated_model_stands_upright(page, live_server_factory):
    # Regression: MDL geometry is Z-up and the loader rotates it -90deg about X
    # into Three.js's Y-up world so figures stand. applyAnimFrame() rewrites the
    # position attribute every load (frame 0) and during playback, so it must
    # apply the SAME rotation or the model reverts to Z-up and lies on its side.
    # 3D orientation has no DOM projection, so read the loaded mesh's geometry
    # bounding box via the debug handle. Bad2 is a 20-frame MDL3 (A5) figure.
    open_editor_with_model(page, live_server_factory("Paper2.MDL", "Bad2.MDL"), "Bad2.MDL")
    size = page.evaluate(
        "() => { const m = window.__model; m.geometry.computeBoundingBox();"
        " const b = m.geometry.boundingBox;"
        " return [b.max.x - b.min.x, b.max.y - b.min.y, b.max.z - b.min.z]; }"
    )
    dx, dy, dz = size
    # Bad2 is a humanoid: its tallest extent is its height. Standing => height
    # is along Y; lying on its side (the bug) => height is along Z.
    assert dy > dz and dy > dx, f"model not upright: extents dx={dx:.1f} dy={dy:.1f} dz={dz:.1f}"


def select_tool(page, tool):
    page.locator(f"#tool-{tool}").click()


def active_tool(page):
    for t in ("brush", "eraser", "fill", "pick"):
        if page.locator(f"#tool-{t}").get_attribute("aria-pressed") == "true":
            return t
    return None


def test_tool_selection_is_mutually_exclusive(page, live_server):
    open_editor(page, live_server)
    assert active_tool(page) == "brush", "brush is the default tool"

    select_tool(page, "eraser")
    assert active_tool(page) == "eraser"
    assert page.locator("#tool-brush").get_attribute("aria-pressed") == "false"

    select_tool(page, "fill")
    assert active_tool(page) == "fill"

    # keyboard shortcut B selects brush. The last-clicked tool button holds
    # focus; letter keys don't activate buttons, so no extra focus step (and no
    # canvas click that would trigger a fill once Task 3 lands).
    page.keyboard.press("b")
    assert active_tool(page) == "brush"
    page.keyboard.press("e")
    assert active_tool(page) == "eraser"
