import io, os, time
from PIL import Image
import server as server_mod
from server import create_app, mtime_changed

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

def client():
    app = create_app(ROOT)
    app.config.update(TESTING=True)
    return app.test_client()

def test_model_endpoint_returns_geometry():
    r = client().get("/api/model?path=samples/Paper2.MDL")
    assert r.status_code == 200
    g = r.get_json()
    assert g["format"] == "IDPO"
    assert len(g["positions"]) == 12 * 3 * 3

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
