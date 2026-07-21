import base64, io, json, os, shutil, time
from PIL import Image
import mdl_tool
import server as server_mod
from server import create_app, mtime_changed

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def client():
    app = create_app(ROOT)
    app.config.update(TESTING=True)
    return app.test_client()

def edit_client(tmp_path, model="Bad2.MDL"):
    # An app rooted at a throwaway dir holding a copy of a sample model, so the
    # extract/save endpoints write _edit/ and _backup_mdl/ under tmp, not the repo.
    shutil.copy(os.path.join(ROOT, "samples", model), tmp_path / model)
    app = create_app(str(tmp_path))
    app.config.update(TESTING=True)
    return app.test_client(), model

def _png_data_url(w, h, color):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _rgba_data_url(w, h):
    buf = io.BytesIO()
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for y in range(h):
        for x in range(w):
            if 2 <= x < w - 2 and 2 <= y < h - 2:
                img.putpixel((x, y), (220, 160, 40, 255))
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def test_model_endpoint_returns_geometry():
    r = client().get("/api/model?path=samples/Paper2.MDL")
    assert r.status_code == 200
    g = r.get_json()
    assert g["format"] == "IDPO"
    assert len(g["positions"]) == 12 * 3 * 3


def test_model_endpoint_can_include_frames():
    r = client().get("/api/model?path=samples/Paper2.MDL&includeFrames=1")
    assert r.status_code == 200
    g = r.get_json()
    assert "frames" in g
    assert len(g["frames"]) == g["numframes"]
    assert len(g["frames"][0]) == len(g["positions"])

def test_skin_endpoint_returns_png():
    r = client().get("/api/skin?path=samples/Paper2.MDL&index=0")
    assert r.status_code == 200
    assert r.mimetype == "image/png"
    img = Image.open(io.BytesIO(r.data))
    assert img.size == (1280, 800)

def test_absolute_path_outside_root_loads():
    # The viewer intentionally opens models anywhere on disk (localhost-only
    # single-user tool), so an absolute path is honored as-is, not root-jailed.
    abs_path = os.path.join(ROOT, "samples", "Paper2.MDL")
    assert os.path.isabs(abs_path)
    r = client().get("/api/model?path=" + abs_path)
    assert r.status_code == 200
    assert r.get_json()["format"] == "IDPO"

def test_nonexistent_path_returns_404():
    r = client().get("/api/model?path=samples/DoesNotExist.MDL")
    assert r.status_code == 404

def test_orientation_defaults_to_no_flip(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "ORIENT_STORE", str(tmp_path / "o.json"))
    r = client().get("/api/orientation?path=samples/Paper2.MDL")
    assert r.status_code == 200
    assert r.get_json() == {"flipV": False}

def test_orientation_persists_and_reads_back(tmp_path, monkeypatch):
    store = str(tmp_path / "o.json")
    monkeypatch.setattr(server_mod, "ORIENT_STORE", store)
    c = client()
    c.post("/api/orientation", json={"path": "samples/Paper2.MDL", "flipV": True})
    assert c.get("/api/orientation?path=samples/Paper2.MDL").get_json() == {"flipV": True}
    # Setting back to the default drops the override so the store holds only
    # genuine exceptions.
    c.post("/api/orientation", json={"path": "samples/Paper2.MDL", "flipV": False})
    assert c.get("/api/orientation?path=samples/Paper2.MDL").get_json() == {"flipV": False}

def test_orientation_keyed_by_absolute_path(tmp_path, monkeypatch):
    # Relative and absolute references to the same model share one override.
    monkeypatch.setattr(server_mod, "ORIENT_STORE", str(tmp_path / "o.json"))
    c = client()
    abs_path = os.path.join(ROOT, "samples", "Paper2.MDL")
    c.post("/api/orientation", json={"path": "samples/Paper2.MDL", "flipV": True})
    assert c.get("/api/orientation?path=" + abs_path).get_json() == {"flipV": True}

def test_extract_creates_working_skin_and_backup(tmp_path):
    c, model = edit_client(tmp_path)
    r = c.post("/api/extract", json={"path": model})
    assert r.status_code == 200
    body = r.get_json()
    skin_rel = body["skin"]
    assert body["skins"] and body["skins"][0] == skin_rel
    assert (tmp_path / skin_rel).is_file()
    # backup exists (filename is dir-hash-prefixed to avoid same-name collisions)
    assert list((tmp_path / "_backup_mdl").glob(f"*-{model}"))

def test_extract_reuses_existing_skin_but_force_resets(tmp_path):
    c, model = edit_client(tmp_path)
    ex = c.post("/api/extract", json={"path": model}).get_json()
    skin = tmp_path / ex["skin"]
    # simulate an in-progress edit
    w, h = Image.open(skin).size
    Image.new("RGB", (w, h), (1, 2, 3)).save(skin)
    # a plain reload must NOT clobber the edit
    r = c.post("/api/extract", json={"path": model})
    assert r.get_json().get("reused") is True
    assert Image.open(skin).getpixel((0, 0)) == (1, 2, 3)
    # force re-extracts a pristine copy from the backup
    r = c.post("/api/extract", json={"path": model, "force": True})
    assert r.get_json().get("reused") is not True
    assert Image.open(skin).getpixel((0, 0)) != (1, 2, 3)

def test_skin_write_confined_to_edit_tree(tmp_path):
    c, model = edit_client(tmp_path)
    ex = c.post("/api/extract", json={"path": model}).get_json()
    ok = c.post("/api/skin-write",
                json={"file": ex["skin"], "png": _png_data_url(1, 1, (10, 20, 30))})
    assert ok.status_code == 200
    # a path escaping _edit/ must be refused; check the *real* traversal target
    # ("../evil.png" resolves to tmp_path.parent), not a location it would never
    # be written to anyway.
    target = tmp_path.parent / "evil.png"
    assert not target.exists()
    bad = c.post("/api/skin-write",
                 json={"file": "../evil.png", "png": _png_data_url(1, 1, (0, 0, 0))})
    assert bad.status_code == 403
    assert not target.exists()

def test_save_roundtrips_edited_skin_into_mdl(tmp_path):
    c, model = edit_client(tmp_path)
    ex = c.post("/api/extract", json={"path": model}).get_json()
    skin_rel = ex["skin"]
    meta = json.load(open(tmp_path / os.path.dirname(skin_rel) / "_meta.json"))
    w, h = meta["skin_w"], meta["skin_h"]
    c.post("/api/skin-write",
           json={"file": skin_rel, "png": _png_data_url(w, h, (200, 40, 60))})
    r = c.post("/api/save", json={"path": model})
    assert r.status_code == 200
    # the saved .MDL now decodes to that solid color (within 565 quantization)
    b = open(tmp_path / model, "rb").read()
    _, skins, *_ = mdl_tool.parse_skins(b)
    sk = skins[0]
    img = mdl_tool.dec_skin(b[sk["doff"]:sk["doff"] + sk["dlen"]], sk["w"], sk["h"], sk["t"])
    px = img.getpixel((w // 2, h // 2))
    assert abs(px[0] - 200) < 12 and abs(px[1] - 40) < 12 and abs(px[2] - 60) < 12
    assert sk["t"] == 2  # re-embedded as RGB565


def test_skin_add_remove_and_save_updates_skin_count(tmp_path):
    c, model = edit_client(tmp_path)
    ex = c.post("/api/extract", json={"path": model}).get_json()
    assert len(ex["skins"]) >= 1
    # Add a second skin cloned from skin0, recolor it, then save.
    add = c.post("/api/skin-add", json={"path": model, "fromIndex": 0})
    assert add.status_code == 200
    add_body = add.get_json()
    assert len(add_body["skins"]) >= 2
    skin1 = add_body["skins"][1]
    c.post("/api/skin-write",
           json={"file": skin1, "png": _png_data_url(640, 400, (20, 120, 240))})
    s = c.post("/api/save", json={"path": model})
    assert s.status_code == 200
    b = open(tmp_path / model, "rb").read()
    _fmt, skins, *_ = mdl_tool.parse_skins(b)
    assert len(skins) >= 2
    # Now remove back to one skin and save again.
    rm = c.post("/api/skin-remove", json={"path": model, "index": 1})
    assert rm.status_code == 200
    assert len(rm.get_json()["skins"]) == len(add_body["skins"]) - 1
    s2 = c.post("/api/save", json={"path": model})
    assert s2.status_code == 200
    b2 = open(tmp_path / model, "rb").read()
    _fmt2, skins2, *_ = mdl_tool.parse_skins(b2)
    assert len(skins2) == len(rm.get_json()["skins"])

def test_save_without_extract_is_rejected(tmp_path):
    c, model = edit_client(tmp_path)
    r = c.post("/api/save", json={"path": model})
    assert r.status_code == 400


def test_upscale_doubles_working_skin_size(tmp_path):
    c, model = edit_client(tmp_path)
    ex = c.post("/api/extract", json={"path": model}).get_json()
    skin = tmp_path / ex["skin"]
    w0, h0 = Image.open(skin).size
    r = c.post("/api/upscale", json={"path": model, "factor": 2, "method": "nearest"})
    assert r.status_code == 200
    w1, h1 = Image.open(skin).size
    assert (w1, h1) == (w0 * 2, h0 * 2)


def test_paper_from_image_generates_idpo_model(tmp_path):
    c = create_app(str(tmp_path)).test_client()
    out_rel = "samples/generated_paper.mdl"
    r = c.post("/api/paper-from-image", json={
        "imageData": _rgba_data_url(48, 48),
        "outPath": out_rel,
        "grid": 8,
        "alphaThreshold": 10,
        "pixelScale": 1.0,
    })
    assert r.status_code == 200
    out_abs = tmp_path / out_rel
    assert out_abs.is_file()
    g = c.get("/api/model?path=" + out_rel).get_json()
    assert g["format"] == "IDPO"
    assert g["numtris"] > 0
    xs = g["positions"][0::3]
    ys = g["positions"][1::3]
    zs = g["positions"][2::3]
    # Flat cutout plane should be Y/Z-facing (X almost constant).
    assert (max(xs) - min(xs)) < 1.0
    assert (max(ys) - min(ys)) > 1.0
    assert (max(zs) - min(zs)) > 1.0


def test_paper_from_image_can_match_reference_height_and_borrow_animation(tmp_path):
    c = create_app(str(tmp_path)).test_client()
    shutil.copy(os.path.join(ROOT, "samples", "PipSid.MDL"), tmp_path / "PipSid.MDL")
    out_rel = "samples/generated_paper_anim.mdl"
    r = c.post("/api/paper-from-image", json={
        "imageData": _rgba_data_url(64, 96),
        "outPath": out_rel,
        "grid": 8,
        "alphaThreshold": 10,
        "pixelScale": 1.0,
        "targetHeightModelPath": "PipSid.MDL",
        "animSourceModelPath": "PipSid.MDL",
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["numframes"] > 1
    ref = c.get("/api/model?path=PipSid.MDL&includeFrames=1").get_json()
    gen = c.get("/api/model?path=" + out_rel + "&includeFrames=1").get_json()
    ref_h = max((max(f[2::3]) - min(f[2::3])) for f in ref["frames"])
    gen_h = max((max(f[2::3]) - min(f[2::3])) for f in gen["frames"])
    assert abs(ref_h - gen_h) < 1.0

def test_mtime_changed_detects_write(tmp_path):
    p = tmp_path / "skin0.png"
    p.write_bytes(b"a")
    changed, m1 = mtime_changed(str(p), 0.0)
    assert changed is True
    changed2, m2 = mtime_changed(str(p), m1)
    assert changed2 is False
    time.sleep(0.01)
    p.write_bytes(b"bb")
    os_mtime = p.stat().st_mtime
    changed3, m3 = mtime_changed(str(p), m1)
    assert changed3 is True and m3 == os_mtime
