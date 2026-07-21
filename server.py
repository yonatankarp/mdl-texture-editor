import io, json, os, sys, time, subprocess
from flask import Flask, request, jsonify, send_file, abort, send_from_directory, Response
import mdl_tool
from mdl_geometry import parse_geometry

STATIC = os.path.join(os.path.dirname(__file__), "static")
# Per-model orientation overrides, keyed by absolute path. The decoder's
# default vertical orientation is correct for most models; this stores the
# exceptions (flat props like the newspaper) the user flips by hand.
ORIENT_STORE = os.path.join(os.path.dirname(__file__), "orientation.json")


def _load_orient():
    try:
        with open(ORIENT_STORE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def _save_orient(data):
    tmp = ORIENT_STORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, ORIENT_STORE)

def _resolve(root, path):
    # Absolute paths are used as-is (a model may live anywhere on disk);
    # relative paths anchor to root so the built-in defaults work regardless
    # of the process CWD. No root-jail: this is a localhost-only single-user
    # tool and is expected to open models outside the repo.
    p = os.path.expanduser(path)
    full = p if os.path.isabs(p) else os.path.join(root, p)
    full = os.path.abspath(full)
    if not os.path.isfile(full):
        abort(404, "not found")
    return full

def mtime_changed(path, last):
    m = os.path.getmtime(path)
    return (m != last, m)

def _skin_image(mdl_path, index):
    b = open(mdl_path, "rb").read()
    fmt, skins, after, nv, nt, nf = mdl_tool.parse_skins(b)
    sk = skins[index]
    return mdl_tool.dec_skin(b[sk["doff"]:sk["doff"] + sk["dlen"]], sk["w"], sk["h"], sk["t"])

def create_app(root):
    app = Flask(__name__, static_folder=None)

    @app.get("/api/model")
    def model():
        full = _resolve(root, request.args["path"])
        try:
            return jsonify(parse_geometry(full))
        except ValueError as e:
            # Unsupported format (MDL2/MDL4) or a model this parser can't
            # decode (degenerate/placeholder). Report cleanly so the UI can
            # explain instead of showing a blank screen.
            return jsonify({"error": str(e)}), 415

    @app.get("/api/orientation")
    def get_orientation():
        key = _resolve(root, request.args["path"])
        flip_v = bool(_load_orient().get(key, {}).get("flipV", False))
        return jsonify({"flipV": flip_v})

    @app.post("/api/orientation")
    def set_orientation():
        body = request.get_json(force=True)
        key = _resolve(root, body["path"])
        flip_v = bool(body.get("flipV", False))
        data = _load_orient()
        if flip_v:
            data.setdefault(key, {})["flipV"] = True
        else:
            # Default orientation: drop the override entirely so the store
            # only ever holds genuine exceptions.
            data.pop(key, None)
        _save_orient(data)
        return jsonify({"flipV": flip_v})

    @app.get("/api/skin")
    def skin():
        index = int(request.args.get("index", 0))
        img = _skin_image(_resolve(root, request.args["path"]), index)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        return send_file(buf, mimetype="image/png")

    @app.get("/api/pngskin")
    def pngskin():
        return send_file(_resolve(root, request.args["file"]), mimetype="image/png")

    @app.get("/api/watch")
    def watch():
        full = _resolve(root, request.args["file"])
        def stream():
            last = 0.0
            while True:
                changed, last = mtime_changed(full, last)
                if changed:
                    yield "data: changed\n\n"
                time.sleep(0.5)
        return Response(stream(), mimetype="text/event-stream")

    @app.get("/api/pick")
    def pick():
        # Native OS file chooser -> returns the absolute path of the picked
        # file (or {cancelled: true}). macOS only, via osascript; other
        # platforms should type an absolute path into the field.
        if sys.platform != "darwin":
            return jsonify({"error": "file picker is macOS-only; type an absolute path instead"}), 501
        script = 'POSIX path of (choose file with prompt "Select an MDL model (.MDL)")'
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if proc.returncode != 0:
            return jsonify({"cancelled": True})
        return jsonify({"path": proc.stdout.strip()})

    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:p>")
    def static_files(p):
        return send_from_directory(STATIC, p)

    return app

if __name__ == "__main__":
    # Relative model paths anchor here; absolute paths open models anywhere.
    root = os.path.dirname(os.path.abspath(__file__))
    create_app(root).run(port=5005, debug=True, threaded=True)
