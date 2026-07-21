"""
mdl_tool.py - extract / import skins for GameStudio A5 models.

Supports:
  MDL5 - per-skin [type][w][h][pixels]; int16 UVs
  MDL3 - shared skin size in header; each skin [type][pixels]; int16 UVs
  IDPO - Quake1-style; shared size; each skin [type][pixels]; int32 onseam,s,t UVs

Skin types:
  0 = 8-bit paletted (tools/game_palette.raw from GFX/palette.pcx)
  2 = RGB565

Import always writes type-2 (565), so 8-bit models are upgraded on re-embed.

Usage:
  python mdl_tool.py extract <Model.MDL> <out_dir>
  python mdl_tool.py import  <Model.MDL> <in_dir> [--out <Model.MDL>]
  python mdl_tool.py info    <Model.MDL>
"""
import struct, os, sys, json, hashlib, re
from PIL import Image

HDR = 84
TYPE_8 = 0
TYPE_565 = 2

# Directory originals are backed up to before a re-embed. The server points
# this at an absolute path so backups don't depend on the process CWD.
BACKUP_DIR = "_backup_mdl"

_PALETTE = None

def game_palette():
    global _PALETTE
    if _PALETTE is not None:
        return _PALETTE
    here = os.path.dirname(os.path.abspath(__file__))
    raw_path = os.path.join(here, "game_palette.raw")
    if os.path.exists(raw_path):
        _PALETTE = open(raw_path, "rb").read()[:768]
    else:
        pcx = os.path.join(here, "..", "GFX", "palette.pcx")
        im = Image.open(pcx)
        _PALETTE = bytes(im.getpalette()[:768])
    return _PALETTE

def magic_of(b):
    return b[:4]

def rd_counts(b):
    numskins, sw, sh, numverts, numtris, numframes = struct.unpack_from("<6i", b, 48)
    return numskins, sw, sh, numverts, numtris, numframes

def fmt_of(b):
    m = magic_of(b)
    try:
        s = m.decode("latin1")
    except Exception:
        s = ""
    if s.startswith("MDL5") or m == b"MDL5":
        return "MDL5"
    if s.startswith("MDL3") or m == b"MDL3":
        return "MDL3"
    if m == b"IDPO":
        return "IDPO"
    raise SystemExit(f"unsupported magic {m!r}")

def skin_bpp(t):
    if t == TYPE_8:
        return 1
    if t == TYPE_565:
        return 2
    return None

def parse_skins(b):
    fmt = fmt_of(b)
    numskins, sw, sh, numverts, numtris, numframes = rd_counts(b)
    skins = []
    off = HDR
    if fmt == "MDL5":
        for i in range(numskins):
            t, w, h = struct.unpack_from("<3i", b, off)
            bpp = skin_bpp(t)
            if bpp is None:
                raise SystemExit(f"skin {i}: unsupported type {t}")
            doff = off + 12
            dlen = w * h * bpp
            skins.append({"t": t, "w": w, "h": h, "doff": doff, "dlen": dlen, "hoff": off})
            off = doff + dlen
    else:
        for i in range(numskins):
            (t,) = struct.unpack_from("<i", b, off)
            bpp = skin_bpp(t)
            if bpp is None:
                raise SystemExit(f"skin {i}: unsupported type {t}")
            doff = off + 4
            dlen = sw * sh * bpp
            skins.append({"t": t, "w": sw, "h": sh, "doff": doff, "dlen": dlen, "hoff": off})
            off = doff + dlen
    return fmt, skins, off, numverts, numtris, numframes

def dec_skin(raw, w, h, t):
    if t == TYPE_565:
        return dec565(raw, w, h)
    if t == TYPE_8:
        img = Image.frombytes("P", (w, h), raw)
        img.putpalette(game_palette())
        return img.convert("RGB")
    raise SystemExit(f"decode: bad type {t}")

def dec565(raw, w, h):
    img = Image.new("RGB", (w, h)); px = img.load()
    for i in range(w * h):
        v = raw[i * 2] | (raw[i * 2 + 1] << 8)
        r = (v >> 11) & 0x1F; g = (v >> 5) & 0x3F; bl = v & 0x1F
        px[i % w, i // w] = ((r * 255) // 31, (g * 255) // 63, (bl * 255) // 31)
    return img

def enc565(img):
    img = img.convert("RGB"); w, h = img.size; px = img.load()
    out = bytearray(w * h * 2)
    for i in range(w * h):
        r, g, bl = px[i % w, i // w]
        v = ((r >> 3) << 11) | ((g >> 2) << 5) | (bl >> 3)
        out[i * 2] = v & 0xFF; out[i * 2 + 1] = (v >> 8) & 0xFF
    return bytes(out), w, h

def backup_path(mdl):
    # Key the backup by the model's directory so two models sharing a filename
    # in different folders (e.g. the game's MDL/ and a mods dir) don't share
    # one backup and clobber each other's pristine original.
    key = hashlib.sha1(os.path.dirname(os.path.abspath(mdl)).encode()).hexdigest()[:8]
    return os.path.join(BACKUP_DIR, f"{key}-{os.path.basename(mdl)}")

def scale_uvs(fmt, uv_bytes, numverts, old_w, old_h, new_w, new_h):
    sx = new_w / old_w; sy = new_h / old_h
    uv = bytearray(uv_bytes)
    if fmt == "IDPO":
        for i in range(numverts):
            onseam, s, t = struct.unpack_from("<3i", uv, i * 12)
            s2 = max(0, min(new_w - 1, int(round(s * sx))))
            t2 = max(0, min(new_h - 1, int(round(t * sy))))
            struct.pack_into("<3i", uv, i * 12, onseam, s2, t2)
    else:
        for i in range(numverts):
            u, v = struct.unpack_from("<2h", uv, i * 4)
            u2 = max(0, min(new_w - 1, int(round(u * sx))))
            v2 = max(0, min(new_h - 1, int(round(v * sy))))
            struct.pack_into("<2h", uv, i * 4, u2, v2)
    return bytes(uv)

def uv_size(fmt, numverts):
    return numverts * (12 if fmt == "IDPO" else 4)

def extract(mdl, outdir):
    b = open(mdl, "rb").read()
    bp = backup_path(mdl)
    os.makedirs(os.path.dirname(bp) or ".", exist_ok=True)
    if not os.path.exists(bp):
        open(bp, "wb").write(b)
        print(f"backed up original -> {bp}")
    b = open(bp, "rb").read()
    fmt, skins, after, numverts, numtris, numframes = parse_skins(b)
    os.makedirs(outdir, exist_ok=True)
    for i, sk in enumerate(skins):
        dec_skin(b[sk["doff"]:sk["doff"] + sk["dlen"]], sk["w"], sk["h"], sk["t"]).save(
            os.path.join(outdir, f"skin{i}.png"))
    meta = {
        "format": fmt,
        "numskins": len(skins),
        "skin_w": skins[0]["w"],
        "skin_h": skins[0]["h"],
        "numverts": numverts,
        "src_types": [sk["t"] for sk in skins],
    }
    json.dump(meta, open(os.path.join(outdir, "_meta.json"), "w"), indent=2)
    print(f"extracted {len(skins)} {fmt} skins ({skins[0]['w']}x{skins[0]['h']}) types={meta['src_types']} -> {outdir}")

def do_import(mdl, indir, out=None):
    bp = backup_path(mdl)
    src = bp if os.path.exists(bp) else mdl
    b = open(src, "rb").read()
    fmt, skins, after, numverts, numtris, numframes = parse_skins(b)
    old_w, old_h = skins[0]["w"], skins[0]["h"]

    picks = []
    for name in os.listdir(indir):
        m = re.match(r"^skin(\d+)\.png$", name, flags=re.IGNORECASE)
        if m:
            picks.append((int(m.group(1)), os.path.join(indir, name)))
    picks.sort(key=lambda x: x[0])
    if not picks:
        raise SystemExit(f"no skin*.png files found in {indir}")
    # Require contiguous numbering to avoid silent ordering mistakes.
    expected = list(range(len(picks)))
    got = [n for n, _p in picks]
    if got != expected:
        raise SystemExit(f"skin files must be contiguous skin0..skinN; found indices {got}")
    numskins = len(picks)

    newskins = []
    nw = nh = None
    for i, p in picks:
        data, w, h = enc565(Image.open(p))
        if nw is None:
            nw, nh = w, h
        elif (w, h) != (nw, nh):
            raise SystemExit(f"all skins must be same size; {p} is {w}x{h} not {nw}x{nh}")
        newskins.append(data)

    uv_len = uv_size(fmt, numverts)
    uv = scale_uvs(fmt, b[after:after + uv_len], numverts, old_w, old_h, nw, nh)
    rest = b[after + uv_len:]

    outb = bytearray(b[:HDR])
    struct.pack_into("<i", outb, 48, numskins)
    if fmt != "MDL5":
        struct.pack_into("<2i", outb, 52, nw, nh)

    for data in newskins:
        if fmt == "MDL5":
            outb += struct.pack("<3i", TYPE_565, nw, nh) + data
        else:
            outb += struct.pack("<i", TYPE_565) + data

    outb += uv + rest
    dst = out or mdl
    open(dst, "wb").write(outb)
    print(f"imported {numskins} {fmt} skins {old_w}x{old_h} -> {nw}x{nh} (as 565); wrote {dst} ({len(outb)} bytes)")

def info(mdl):
    b = open(mdl, "rb").read()
    fmt, skins, after, numverts, numtris, numframes = parse_skins(b)
    types = [sk["t"] for sk in skins]
    print(f"{mdl}: {fmt} skins={len(skins)} {skins[0]['w']}x{skins[0]['h']} "
          f"types={types} verts={numverts} tris={numtris} frames={numframes}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "info":
        info(sys.argv[2])
    elif cmd == "extract":
        extract(sys.argv[2], sys.argv[3])
    elif cmd == "import":
        out = None
        if "--out" in sys.argv:
            out = sys.argv[sys.argv.index("--out") + 1]
        do_import(sys.argv[2], sys.argv[3], out)
    else:
        print("unknown command", cmd); sys.exit(1)
