"""Generate a flat IDPO MDL from a 2D image cutout."""
import math
import os
import struct

from PIL import Image

from mdl_geometry import parse_geometry
import mdl_tool


def _sample_alpha(rgba, x, y):
    return rgba.getpixel((x, y))[3]


def _build_cutout_mesh(rgba, grid, alpha_threshold, pixel_scale):
    w, h = rgba.size
    active = []
    for y0 in range(0, h, grid):
        for x0 in range(0, w, grid):
            cx = min(w - 1, x0 + grid // 2)
            cy = min(h - 1, y0 + grid // 2)
            if _sample_alpha(rgba, cx, cy) >= alpha_threshold:
                active.append((x0, y0, min(w, x0 + grid), min(h, y0 + grid)))
    if not active:
        raise ValueError("no opaque pixels passed the alpha threshold")

    verts = []
    stverts = []
    vmap = {}

    def add_corner(px, py):
        key = (px, py)
        idx = vmap.get(key)
        if idx is not None:
            return idx
        idx = len(verts)
        vmap[key] = idx
        # Characters in this pipeline face +X. Build the cutout in Y/Z so its
        # normal points along +/-X instead of +/-Y (which rendered edge-on
        # in-game for some cameras).
        y = (px - (w / 2.0)) * pixel_scale
        z = ((h / 2.0) - py) * pixel_scale
        verts.append((0.0, y, z))
        stverts.append((0, int(round(px)), int(round(py))))
        return idx

    tris = []
    for x0, y0, x1, y1 in active:
        v00 = add_corner(x0, y0)
        v10 = add_corner(x1, y0)
        v11 = add_corner(x1, y1)
        v01 = add_corner(x0, y1)
        # facesfront=1 so IDPO seam behavior stays simple. Emit both windings:
        # some game paths cull backfaces hard, and 2D cutouts should remain
        # visible from either side.
        tris.append((1, v00, v11, v10))
        tris.append((1, v00, v01, v11))
        tris.append((1, v10, v11, v00))
        tris.append((1, v11, v01, v00))
    return verts, stverts, tris


def _pack_frame_vertices(verts):
    mins = [min(v[i] for v in verts) for i in range(3)]
    maxs = [max(v[i] for v in verts) for i in range(3)]
    ranges = [maxs[i] - mins[i] for i in range(3)]
    scale = [max(r / 255.0, 1e-6) for r in ranges]
    trans = mins

    packed = []
    for x, y, z in verts:
        px = int(round((x - trans[0]) / scale[0]))
        py = int(round((y - trans[1]) / scale[1]))
        pz = int(round((z - trans[2]) / scale[2]))
        packed.append((
            max(0, min(255, px)),
            max(0, min(255, py)),
            max(0, min(255, pz)),
        ))
    return scale, trans, packed


def _bbox_center_and_height(flat_positions):
    xs = flat_positions[0::3]
    ys = flat_positions[1::3]
    zs = flat_positions[2::3]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    cz = (min(zs) + max(zs)) / 2.0
    h = max(zs) - min(zs)
    return (cx, cy, cz), h


def _verts_bbox(verts):
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    zs = [v[2] for v in verts]
    return (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))


def _apply_uniform_scale_about_center(verts, factor):
    mnx, mxx, mny, mxy, mnz, mxz = _verts_bbox(verts)
    cx = (mnx + mxx) / 2.0
    cy = (mny + mxy) / 2.0
    cz = (mnz + mxz) / 2.0
    out = []
    for x, y, z in verts:
        out.append((
            cx + (x - cx) * factor,
            cy + (y - cy) * factor,
            cz + (z - cz) * factor,
        ))
    return out


def _max_frame_height(ref_model_path):
    g = parse_geometry(ref_model_path, include_frames=True)
    frames = g.get("frames") or [g["positions"]]
    heights = []
    for f in frames:
        _c, h = _bbox_center_and_height(f)
        heights.append(h)
    return max(heights) if heights else 0.0


def _donor_frame_transforms(anim_source_path):
    """Return per-frame (sx, tx, ty, tz) transforms from donor frame stats."""
    g = parse_geometry(anim_source_path, include_frames=True)
    frames = g.get("frames") or [g["positions"]]
    if not frames:
        return [(1.0, 0.0, 0.0, 0.0)]
    c0, h0 = _bbox_center_and_height(frames[0])
    if h0 <= 1e-6:
        h0 = 1.0
    out = []
    for f in frames:
        c, h = _bbox_center_and_height(f)
        sx = h / h0 if h > 1e-6 else 1.0
        out.append((sx, c[0] - c0[0], c[1] - c0[1], c[2] - c0[2]))
    return out


def _make_frame_variants(base_verts, donor_transforms):
    """Apply donor frame transforms to the generated base mesh."""
    if not donor_transforms:
        return [base_verts]
    mnx, mxx, mny, mxy, mnz, mxz = _verts_bbox(base_verts)
    cx = (mnx + mxx) / 2.0
    cy = (mny + mxy) / 2.0
    cz = (mnz + mxz) / 2.0
    frames = []
    for sx, tx, ty, tz in donor_transforms:
        f = []
        for x, y, z in base_verts:
            nx = cx + (x - cx) * sx + tx
            ny = cy + (y - cy) * sx + ty
            nz = cz + (z - cz) * sx + tz
            f.append((nx, ny, nz))
        frames.append(f)
    return frames


def _write_idpo(path, skin_raw, skin_w, skin_h, stverts, tris, frames_verts):
    first_verts = frames_verts[0]
    scale, trans, packed0 = _pack_frame_vertices(first_verts)
    numverts = len(first_verts)
    numtris = len(tris)
    numframes = len(frames_verts)
    diag = math.sqrt(sum((max(v[i] for v in first_verts) - min(v[i] for v in first_verts)) ** 2 for i in range(3)))
    header = struct.pack(
        "<4si3f3ff3f8if",
        b"IDPO",
        6,
        scale[0], scale[1], scale[2],
        trans[0], trans[1], trans[2],
        diag / 2.0,
        0.0, 0.0, 0.0,
        1,  # numskins
        skin_w,
        skin_h,
        numverts,
        numtris,
        numframes,
        0,  # synctype
        0,  # flags
        diag,
    )

    outb = bytearray(header)
    outb += struct.pack("<i", mdl_tool.TYPE_565) + skin_raw
    for onseam, s, t in stverts:
        outb += struct.pack("<3i", onseam, s, t)
    for facesfront, a, b, c in tris:
        outb += struct.pack("<4i", facesfront, a, b, c)

    for frame_idx, verts in enumerate(frames_verts):
        _s, _t, packed = _pack_frame_vertices(verts)
        outb += struct.pack("<i", 0)  # frame type: single
        minx = min(p[0] for p in packed)
        miny = min(p[1] for p in packed)
        minz = min(p[2] for p in packed)
        maxx = max(p[0] for p in packed)
        maxy = max(p[1] for p in packed)
        maxz = max(p[2] for p in packed)
        outb += struct.pack("<4B", minx, miny, minz, 0)
        outb += struct.pack("<4B", maxx, maxy, maxz, 0)
        outb += (f"frame{frame_idx}").encode("ascii", errors="ignore")[:16].ljust(16, b"\x00")
        for px, py, pz in packed:
            outb += struct.pack("<4B", px, py, pz, 0)

    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(outb)


def write_paper_idpo_from_image(
    image_path,
    out_path,
    grid=8,
    alpha_threshold=10,
    pixel_scale=1.0,
    target_height_model_path=None,
    anim_source_model_path=None,
):
    img = Image.open(image_path).convert("RGBA")
    skin_w, skin_h = img.size
    rgba = img
    rgb = Image.new("RGB", rgba.size, (0, 0, 0))
    rgb.paste(rgba, mask=rgba.split()[3])
    skin_raw, _, _ = mdl_tool.enc565(rgb)

    verts, stverts, tris = _build_cutout_mesh(rgba, max(1, int(grid)), int(alpha_threshold), float(pixel_scale))
    matched_height = None
    if target_height_model_path:
        target_h = _max_frame_height(target_height_model_path)
        if target_h > 1e-6:
            _mnx, _mxx, _mny, _mxy, mnz, mxz = _verts_bbox(verts)
            src_h = mxz - mnz
            if src_h > 1e-6:
                verts = _apply_uniform_scale_about_center(verts, target_h / src_h)
                matched_height = target_h

    donor_frames = None
    if anim_source_model_path:
        donor_frames = _donor_frame_transforms(anim_source_model_path)
    frames_verts = _make_frame_variants(verts, donor_frames)
    numverts = len(verts)
    numtris = len(tris)
    _write_idpo(out_path, skin_raw, skin_w, skin_h, stverts, tris, frames_verts)

    return {
        "out": os.path.abspath(out_path),
        "skin_w": skin_w,
        "skin_h": skin_h,
        "numverts": numverts,
        "numtris": numtris,
        "numframes": len(frames_verts),
        "heightMatchedTo": os.path.abspath(target_height_model_path) if matched_height is not None else None,
        "animationSource": os.path.abspath(anim_source_model_path) if anim_source_model_path else None,
    }
