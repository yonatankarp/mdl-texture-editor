import base64, hashlib, io, json, os, sys, time, subprocess
from flask import Flask, request, jsonify, send_file, abort, send_from_directory, Response
from PIL import Image
import mdl_tool
from mdl_geometry import parse_geometry
from mdl_paper import write_paper_idpo_from_image

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

def _workdir(root, model_full):
    # Per-model editable-skin working dir under root/_edit/<stem>-<key>. Both
    # the external editor and the in-browser canvas read/write skin0.png here,
    # and the file watcher points at it, so all edit paths share one pipeline.
    # The dir-hash key keeps two same-named models in different folders apart.
    stem = os.path.splitext(os.path.basename(model_full))[0]
    key = hashlib.sha1(os.path.dirname(os.path.abspath(model_full)).encode()).hexdigest()[:8]
    return os.path.join(root, "_edit", f"{stem}-{key}")


def mtime_changed(path, last):
    m = os.path.getmtime(path)
    return (m != last, m)

def _skin_image(mdl_path, index):
    b = open(mdl_path, "rb").read()
    fmt, skins, after, nv, nt, nf = mdl_tool.parse_skins(b)
    sk = skins[index]
    return mdl_tool.dec_skin(b[sk["doff"]:sk["doff"] + sk["dlen"]], sk["w"], sk["h"], sk["t"])


def _skin_files(workdir):
    pairs = []
    for name in os.listdir(workdir):
        if not (name.lower().startswith("skin") and name.lower().endswith(".png")):
            continue
        mid = name[4:-4]
        if mid.isdigit():
            pairs.append((int(mid), name))
    pairs.sort(key=lambda x: x[0])
    return [name for _idx, name in pairs]


def _skin_rel_list(root, workdir):
    return [os.path.relpath(os.path.join(workdir, n), root) for n in _skin_files(workdir)]


def _renumber_skin_files(workdir):
    names = _skin_files(workdir)
    for i, name in enumerate(names):
        src = os.path.join(workdir, name)
        tmp = os.path.join(workdir, f"__tmp_skin_{i}.png")
        os.replace(src, tmp)
    for i in range(len(names)):
        tmp = os.path.join(workdir, f"__tmp_skin_{i}.png")
        dst = os.path.join(workdir, f"skin{i}.png")
        os.replace(tmp, dst)

def create_app(root):
    app = Flask(__name__, static_folder=None)
    # Absolute backup dir so mdl_tool's extract/import don't depend on the
    # process CWD (the server is threaded; there is no chdir dance).
    mdl_tool.BACKUP_DIR = os.path.abspath(os.path.join(root, "_backup_mdl"))

    @app.get("/api/model")
    def model():
        full = _resolve(root, request.args["path"])
        include_frames = request.args.get("includeFrames", "0") in ("1", "true", "True")
        try:
            return jsonify(parse_geometry(full, include_frames=include_frames))
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

    @app.post("/api/extract")
    def extract():
        # Extract the model's skin(s) to its working dir so they can be edited
        # externally or in-browser. Non-destructive by default: if a working
        # skin already exists it is reused, so reloading a model never wipes
        # in-progress edits. Pass force=true to re-extract a pristine copy from
        # the backup (a "reset skin" action).
        body = request.get_json(force=True)
        full = _resolve(root, body["path"])
        workdir = _workdir(root, full)
        skin0 = os.path.join(workdir, "skin0.png")
        if os.path.exists(skin0) and not body.get("force"):
            skins = _skin_rel_list(root, workdir)
            return jsonify({"skin": os.path.relpath(skin0, root), "skins": skins, "dir": workdir, "reused": True})
        try:
            mdl_tool.extract(full, workdir)
        except (SystemExit, Exception) as e:
            return jsonify({"error": str(e)}), 400
        skin = os.path.relpath(os.path.join(workdir, "skin0.png"), root)
        return jsonify({"skin": skin, "skins": _skin_rel_list(root, workdir), "dir": workdir})

    @app.get("/api/skins")
    def skins():
        full = _resolve(root, request.args["path"])
        workdir = _workdir(root, full)
        if not os.path.isdir(workdir):
            abort(400, "no extracted skin; load the model first")
        return jsonify({"skins": _skin_rel_list(root, workdir), "dir": workdir})

    @app.post("/api/skin-write")
    def skin_write():
        # Overwrite a working skin PNG (used by the in-browser canvas). The
        # file watcher picks up the change and re-textures the model. Confined
        # to the _edit tree so it can't clobber arbitrary files.
        body = request.get_json(force=True)
        full = os.path.abspath(os.path.join(root, body["file"]))
        edit_root = os.path.join(root, "_edit") + os.sep
        if not full.startswith(edit_root):
            abort(403, "skin-write only allowed under _edit/")
        raw = body["png"].split(",", 1)[-1]  # strip optional data-URL prefix
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(base64.b64decode(raw))
        return jsonify({"ok": True})

    @app.post("/api/skin-add")
    def skin_add():
        body = request.get_json(force=True)
        full = _resolve(root, body["path"])
        workdir = _workdir(root, full)
        if not os.path.isdir(workdir):
            abort(400, "no extracted skin; load the model first")
        skins = _skin_files(workdir)
        if not skins:
            abort(400, "no working skins found")
        src_idx = int(body.get("fromIndex", len(skins) - 1))
        src_idx = max(0, min(src_idx, len(skins) - 1))
        src = os.path.join(workdir, skins[src_idx])
        dst = os.path.join(workdir, f"skin{len(skins)}.png")
        Image.open(src).save(dst)
        rel = _skin_rel_list(root, workdir)
        return jsonify({"ok": True, "skins": rel, "index": len(rel) - 1})

    @app.post("/api/skin-remove")
    def skin_remove():
        body = request.get_json(force=True)
        full = _resolve(root, body["path"])
        workdir = _workdir(root, full)
        if not os.path.isdir(workdir):
            abort(400, "no extracted skin; load the model first")
        names = _skin_files(workdir)
        if len(names) <= 1:
            abort(400, "cannot remove the last skin")
        idx = int(body.get("index", len(names) - 1))
        idx = max(0, min(idx, len(names) - 1))
        os.remove(os.path.join(workdir, names[idx]))
        _renumber_skin_files(workdir)
        rel = _skin_rel_list(root, workdir)
        return jsonify({"ok": True, "skins": rel, "index": min(idx, len(rel) - 1)})

    @app.post("/api/upscale")
    def upscale():
        # Upscale the working skin in place, then let /api/watch hot-reload it.
        body = request.get_json(force=True)
        full = _resolve(root, body["path"])
        workdir = _workdir(root, full)
        skin0 = os.path.join(workdir, "skin0.png")
        if not os.path.isfile(skin0):
            abort(400, "no extracted skin; load the model first")
        factor = max(1, int(body.get("factor", 2)))
        method = str(body.get("method", "nearest")).lower()
        if method == "lanczos":
            filt = Image.Resampling.LANCZOS
        elif method in ("bilinear", "linear"):
            filt = Image.Resampling.BILINEAR
        else:
            filt = Image.Resampling.NEAREST
        img = Image.open(skin0).convert("RGB")
        out = img.resize((img.width * factor, img.height * factor), filt)
        out.save(skin0)
        return jsonify({"ok": True, "w": out.width, "h": out.height, "factor": factor, "method": method})

    @app.post("/api/paper-from-image")
    def paper_from_image():
        # Build a flat cutout IDPO model from an RGBA source image.
        body = request.get_json(force=True)
        out_path = body.get("outPath")
        if not out_path:
            abort(400, "missing outPath")
        out_abs = os.path.abspath(out_path if os.path.isabs(out_path) else os.path.join(root, out_path))
        image_path = None
        tmp = None
        if body.get("imagePath"):
            image_path = os.path.abspath(body["imagePath"] if os.path.isabs(body["imagePath"]) else os.path.join(root, body["imagePath"]))
        elif body.get("imageData"):
            raw = body["imageData"].split(",", 1)[-1]
            tmp = out_abs + ".source.png"
            os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(base64.b64decode(raw))
            image_path = tmp
        else:
            abort(400, "missing imagePath or imageData")
        target_height_model = body.get("targetHeightModelPath")
        if target_height_model:
            target_height_model = _resolve(root, target_height_model)
        anim_source_model = body.get("animSourceModelPath")
        if anim_source_model:
            anim_source_model = _resolve(root, anim_source_model)
        try:
            info = write_paper_idpo_from_image(
                image_path=image_path,
                out_path=out_abs,
                grid=max(1, int(body.get("grid", 8))),
                alpha_threshold=max(0, min(255, int(body.get("alphaThreshold", 10)))),
                pixel_scale=float(body.get("pixelScale", 1.0)),
                target_height_model_path=target_height_model,
                anim_source_model_path=anim_source_model,
            )
            return jsonify({"ok": True, **info})
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    @app.post("/api/save")
    def save():
        # Re-embed the edited working skin into the .MDL. do_import rebuilds
        # from the pristine backup (made at extract time), so repeated saves
        # never compound; the original stays recoverable in _backup_mdl/.
        full = _resolve(root, request.get_json(force=True)["path"])
        workdir = _workdir(root, full)
        if not os.path.isdir(workdir):
            abort(400, "no extracted skin; load the model first")
        try:
            mdl_tool.do_import(full, workdir)
        except (SystemExit, Exception) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "backup": os.path.relpath(mdl_tool.backup_path(full), root)})

    @app.post("/api/reveal")
    def reveal():
        # Open the working-skin folder in the OS file browser (macOS only) so
        # the user can find the PNG to edit externally.
        if sys.platform != "darwin":
            return jsonify({"error": "reveal is macOS-only"}), 501
        full = _resolve(root, request.get_json(force=True)["path"])
        workdir = _workdir(root, full)
        subprocess.run(["open", workdir], check=False)
        return jsonify({"ok": True})

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
