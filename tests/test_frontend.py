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
