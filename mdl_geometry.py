"""MDL geometry decoder for the texture editor.

Supports the three GameStudio A5 / Quake-lineage model formats used by the
game: IDPO (Quake1-style) and A5's MDL3 / MDL5.

Always produces NON-INDEXED geometry: 3 independent corners per triangle, each
with its own resolved (u, v). This is required because these formats share one
vertex position across two texture coordinates at seams (IDPO via the onseam
flag; MDL3/MDL5 via separate skin-vertex indices per corner), which indexed GPU
geometry cannot represent without tearing.

Return shape (all formats):
    {format, numtris, skin_w, skin_h, positions[3*3*numtris], uvs[2*3*numtris]}
"""
import struct

HDR = 84


def parse_geometry(path):
    b = open(path, "rb").read()
    magic = b[:4]
    if magic == b"IDPO":
        return _parse_idpo(b)
    if magic in (b"MDL5", b"MDL3"):
        return _parse_a5(b, magic)
    raise ValueError(
        f"unsupported MDL format: magic={magic!r} (only IDPO, MDL3, MDL5)"
    )


def _parse_idpo(b):
    scale = struct.unpack_from("<3f", b, 8)
    trans = struct.unpack_from("<3f", b, 20)
    numskins, skin_w, skin_h, numverts, numtris, numframes = struct.unpack_from("<6i", b, 48)

    off = HDR
    # skip skins
    for _ in range(numskins):
        (t,) = struct.unpack_from("<i", b, off)
        bpp = 2 if t == 2 else 1
        off += 4 + skin_w * skin_h * bpp

    # stverts: (onseam, s, t) int32
    stverts = []
    for i in range(numverts):
        stverts.append(struct.unpack_from("<3i", b, off + i * 12))
    off += numverts * 12

    # triangles: (facesfront, v0, v1, v2) int32
    tris = []
    for i in range(numtris):
        tris.append(struct.unpack_from("<4i", b, off + i * 16))
    off += numtris * 16

    # frame 0 vertex positions (packed uint8 x,y,z + normal index)
    (ftype,) = struct.unpack_from("<i", b, off)
    voff = off + 4
    if ftype != 0:
        # frame group: numframes int32, min trivertx(4), max trivertx(4),
        # times[n] float, then frames; take the first sub-frame.
        (n,) = struct.unpack_from("<i", b, voff)
        voff += 4 + 4 + 4 + n * 4
    # skip bboxmin trivertx(4), bboxmax trivertx(4), name(16)
    voff += 4 + 4 + 16
    packed = []
    for i in range(numverts):
        packed.append(struct.unpack_from("<4B", b, voff + i * 4)[:3])

    positions = []
    uvs = []
    for facesfront, a, bb, c in tris:
        for vi in (a, bb, c):
            px, py, pz = packed[vi]
            positions.extend((
                scale[0] * px + trans[0],
                -(scale[1] * py + trans[1]),  # negate Y: Quake left-handed -> right-handed
                scale[2] * pz + trans[2],
            ))
            onseam, s, t = stverts[vi]
            if onseam and not facesfront:
                s += skin_w // 2
            # Flip V so the skin's top row maps to the model's top. Under the
            # viewer's flipY=true texture, an unflipped V renders 334 of the
            # 375 IDPO models (all standing figures) upside-down; the ~41
            # exceptions are flat props (papers, vases) corrected by the
            # per-model orientation toggle. Matches the A5 path.
            uvs.extend(((s + 0.5) / skin_w, 1.0 - (t + 0.5) / skin_h))

    return {
        "format": "IDPO",
        "numtris": numtris,
        "skin_w": skin_w,
        "skin_h": skin_h,
        "positions": positions,
        "uvs": uvs,
    }


# A5 skin color types -> bytes per pixel.
_A5_BPP = {0: 1, 2: 2, 3: 3, 4: 4}


def _a5_skin_block_end(b, magic, numskins, sw, sh):
    """Offset where geometry begins, found by walking the skin block forward.

    More robust than computing from the file tail: the tail approach must
    assume a per-frame size, which varies, whereas the skin block is
    self-describing. MDL5 stores per-skin dimensions; MDL3 shares the header's.
    """
    off = HDR
    for _ in range(numskins):
        if magic == b"MDL5":
            t, w, h = struct.unpack_from("<3i", b, off)
            off += 12
        else:
            (t,) = struct.unpack_from("<i", b, off)
            w, h = sw, sh
            off += 4
        bpp = _A5_BPP.get(t)
        if bpp is None:
            raise ValueError(f"unsupported {magic.decode()} skin type {t}")
        off += w * h * bpp
    return off


def _parse_a5(b, magic):
    scale = struct.unpack_from("<3f", b, 8)
    trans = struct.unpack_from("<3f", b, 20)
    # A5 adds a 7th count, num_stverts, in Quake's synctype slot (offset 72).
    numskins, sw, sh, numverts, numtris, numframes, num_stverts, _ = struct.unpack_from("<8i", b, 48)

    geo = _a5_skin_block_end(b, magic, numskins, sw, sh)

    # skin dimensions for UV normalization: MDL5 carries them per-skin, MDL3 in
    # the header.
    if magic == b"MDL5":
        _, skin_w, skin_h = struct.unpack_from("<3i", b, HDR)
    else:
        skin_w, skin_h = sw, sh

    # skin vertices: (u, v) int16, num_stverts of them
    stverts = []
    for i in range(num_stverts):
        stverts.append(struct.unpack_from("<2h", b, geo + i * 4))

    # triangles: 3 position-vertex indices then 3 skin-vertex indices, int16
    tri_off = geo + num_stverts * 4
    tris = []
    for i in range(numtris):
        tris.append(struct.unpack_from("<6h", b, tri_off + i * 12))

    # frame 0 vertex positions. Vertex is uint16 x,y,z (+2) for MDL5, uint8
    # x,y,z (+1) for MDL3. Frame header = type(4) + bbox_min + bbox_max +
    # name(16), where each bbox corner is one vertex-sized record.
    vsz = 8 if magic == b"MDL5" else 4
    voff = tri_off + numtris * 12 + 4 + 2 * vsz + 16
    verts = []
    if magic == b"MDL5":
        for i in range(numverts):
            x, y, z, _n = struct.unpack_from("<4H", b, voff + i * 8)
            verts.append((x, y, z))
    else:
        for i in range(numverts):
            x, y, z, _n = struct.unpack_from("<4B", b, voff + i * 4)
            verts.append((x, y, z))

    positions = []
    uvs = []
    for tri in tris:
        xyz_idx = tri[0:3]
        st_idx = tri[3:6]
        for k in range(3):
            vi = xyz_idx[k]
            si = st_idx[k]
            if not (0 <= vi < numverts and 0 <= si < num_stverts):
                # Degenerate/placeholder models (collision hulls, test cubes)
                # carry indices that don't match their declared counts. Reject
                # cleanly rather than emit garbage geometry.
                raise ValueError(
                    f"{magic.decode()}: vertex index out of range "
                    f"(pos {vi}/{numverts}, skin {si}/{num_stverts})"
                )
            x, y, z = verts[vi]
            positions.extend((
                scale[0] * x + trans[0],
                scale[1] * y + trans[1],
                scale[2] * z + trans[2],
            ))
            u, v = stverts[si]
            # A5 skin coordinates use the opposite vertical origin from IDPO,
            # so V is flipped (otherwise the texture maps head-to-foot inverted).
            uvs.extend(((u + 0.5) / skin_w, 1.0 - (v + 0.5) / skin_h))

    return {
        "format": magic.decode(),
        "numtris": numtris,
        "skin_w": skin_w,
        "skin_h": skin_h,
        "positions": positions,
        "uvs": uvs,
    }
