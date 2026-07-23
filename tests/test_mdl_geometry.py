import os
import pytest
from mdl_geometry import apply_vertex_deltas, parse_geometry

MDL = os.path.join(os.path.dirname(__file__), "..", "samples")
FIXTURE = os.path.join(MDL, "Paper2.MDL")


def test_parses_idpo_paper2_non_indexed():
    g = parse_geometry(FIXTURE)
    assert g["format"] == "IDPO"
    assert g["numtris"] == 12
    assert g["numframes"] >= 1
    assert g["skin_w"] == 1280 and g["skin_h"] == 800
    # non-indexed: 3 corners per triangle
    assert len(g["positions"]) == 12 * 3 * 3
    assert len(g["uvs"]) == 12 * 3 * 2


def test_uvs_normalized_within_unit_square():
    g = parse_geometry(FIXTURE)
    us = g["uvs"][0::2]
    vs = g["uvs"][1::2]
    assert all(0.0 <= u <= 1.0 for u in us)
    assert all(0.0 <= v <= 1.0 for v in vs)
    # seam back-faces pushed into right half of the texture
    assert sum(1 for u in us if u > 0.5) == 30


def test_idpo_negates_y_for_right_handed_space():
    # IDPO models are Quake left-handed; the decoder negates Y to convert to
    # Three.js right-handed space (otherwise flat models like the newspaper
    # render mirrored — verified via readable Hebrew text). This one line has
    # been added/removed several times, so pin the sign of a known vertex.
    g = parse_geometry(FIXTURE)
    assert g["positions"][1] == pytest.approx(-17.0, abs=0.01)


def test_idpo_flips_v_by_default():
    # IDPO V is flipped so the skin top maps to the model top (correct for the
    # 334 standing figures; flat props are handled by the per-model toggle).
    # Pin the flipped value of a known corner so the default can't silently
    # revert.
    g = parse_geometry(FIXTURE)
    assert g["uvs"][1] == pytest.approx(0.9744, abs=0.001)


def test_parses_mdl5_non_indexed():
    g = parse_geometry(os.path.join(MDL, "PipSid.MDL"))
    assert g["format"] == "MDL5"
    assert g["numtris"] == 602
    assert g["numframes"] >= 1
    assert g["skin_w"] == 640 and g["skin_h"] == 400
    assert len(g["positions"]) == 602 * 3 * 3
    assert len(g["uvs"]) == 602 * 3 * 2
    assert all(0.0 <= c <= 1.0 for c in g["uvs"])


def test_parses_mdl3_non_indexed():
    g = parse_geometry(os.path.join(MDL, "Bad2.MDL"))
    assert g["format"] == "MDL3"
    assert g["numtris"] == 640
    assert g["numframes"] >= 1
    assert g["skin_w"] == 640 and g["skin_h"] == 400
    assert len(g["positions"]) == 640 * 3 * 3
    assert len(g["uvs"]) == 640 * 3 * 2
    assert all(0.0 <= c <= 1.0 for c in g["uvs"])


def test_rejects_unknown_format(tmp_path):
    p = tmp_path / "fake.mdl"
    p.write_bytes(b"XXXX" + b"\x00" * 200)
    with pytest.raises(ValueError):
        parse_geometry(str(p))


def test_rejects_degenerate_a5_model():
    # cube.mdl declares counts that don't match its triangle indices; must be
    # rejected cleanly rather than emit garbage or crash.
    with pytest.raises(ValueError):
        parse_geometry(os.path.join(MDL, "cube.mdl"))


def test_include_frames_returns_full_frame_arrays():
    g = parse_geometry(FIXTURE, include_frames=True)
    assert "frames" in g
    assert len(g["frames"]) == g["numframes"]
    assert len(g["frames"][0]) == len(g["positions"])


# --- vertex-editing support (issue #22) ---

@pytest.mark.parametrize("name", ["Paper2.MDL", "PipSid.MDL", "Bad2.MDL"])
def test_vertex_indices_map_each_corner_to_its_mdl_vertex(name):
    g = parse_geometry(os.path.join(MDL, name))
    vi = g["vertexIndices"]
    assert len(vi) == g["numtris"] * 3
    assert all(0 <= i < g["numverts"] for i in vi)
    # corners sharing an MDL vertex must resolve to identical positions
    by_vertex = {}
    for k, i in enumerate(vi):
        xyz = tuple(g["positions"][3 * k:3 * k + 3])
        assert by_vertex.setdefault(i, xyz) == xyz


@pytest.mark.parametrize("name,vmax", [
    ("Paper2.MDL", 255), ("PipSid.MDL", 65535), ("Bad2.MDL", 255),
])
def test_quant_affine_reproduces_positions_on_integer_grid(name, vmax):
    g = parse_geometry(os.path.join(MDL, name))
    q = g["quant"]
    assert q["max"] == vmax
    a, b = q["a"], q["b"]
    for k in range(0, len(g["positions"]), 3):
        for ax in range(3):
            v = g["positions"][k + ax]
            p = (v - b[ax]) / a[ax]
            assert abs(p - round(p)) < 1e-3
            assert -0.5 <= p <= vmax + 0.5


@pytest.mark.parametrize("name", ["Paper2.MDL", "PipSid.MDL", "Bad2.MDL"])
def test_sample_models_have_no_grouped_frames(name):
    g = parse_geometry(os.path.join(MDL, name))
    assert g["framesGrouped"] is False


def test_a5_frame_block_size_mismatch_is_flagged(tmp_path):
    # A5 ftype is not a group marker; the write-back safety check is that the
    # frame block fills the file at the simple stride. Trailing bytes break
    # that assumption, so the model must be flagged non-editable.
    p = tmp_path / "trailing.mdl"
    p.write_bytes(open(os.path.join(MDL, "PipSid.MDL"), "rb").read() + b"\x00" * 4)
    assert parse_geometry(str(p))["framesGrouped"] is True


def test_grouped_idpo_frames_are_flagged(tmp_path):
    p = tmp_path / "grouped.mdl"
    p.write_bytes(make_grouped_idpo())
    g = parse_geometry(str(p))
    assert g["framesGrouped"] is True


def make_grouped_idpo():
    """Minimal IDPO model: 1 skin, 3 verts, 1 tri, one GROUPED frame."""
    import struct
    scale = (1.0, 1.0, 1.0)
    trans = (0.0, 0.0, 0.0)
    sw, sh = 4, 4
    hdr = bytearray(84)
    hdr[0:4] = b"IDPO"
    struct.pack_into("<i", hdr, 4, 6)  # version
    struct.pack_into("<3f", hdr, 8, *scale)
    struct.pack_into("<3f", hdr, 20, *trans)
    struct.pack_into("<6i", hdr, 48, 1, sw, sh, 3, 1, 1)
    out = bytearray(hdr)
    out += struct.pack("<i", 0) + bytes(sw * sh)  # 8-bit skin
    for s, t in ((0, 0), (1, 0), (0, 1)):         # stverts
        out += struct.pack("<3i", 0, s, t)
    out += struct.pack("<4i", 1, 0, 1, 2)         # triangle
    # grouped frame: ftype=1, numsub=1, min/max trivertx, times, one sub-frame
    out += struct.pack("<i", 1)
    out += struct.pack("<i", 1)
    out += bytes(4) + bytes(4)                    # group bbox min/max
    out += struct.pack("<f", 0.1)                 # times[1]
    out += bytes(4) + bytes(4) + b"sub".ljust(16, b"\x00")  # frame bbox+name
    for x, y, z in ((10, 20, 30), (40, 50, 60), (70, 80, 90)):
        out += struct.pack("<4B", x, y, z, 0)
    return bytes(out)


# --- apply_vertex_deltas ---

def corners_of(g, vi):
    return [k for k, i in enumerate(g["vertexIndices"]) if i == vi]


def inward_grid_delta(g, vi, k=3):
    # k packed steps per axis, directed toward the bbox middle so the move
    # can't clamp (sample vertices legitimately sit on the bbox surface).
    a, b, vmax = g["quant"]["a"], g["quant"]["b"], g["quant"]["max"]
    corner = corners_of(g, vi)[0]
    d = []
    for ax in range(3):
        p = round((g["positions"][3 * corner + ax] - b[ax]) / a[ax])
        d.append((k if p < vmax / 2 else -k) * a[ax])
    return d


@pytest.mark.parametrize("name", ["Paper2.MDL", "PipSid.MDL", "Bad2.MDL"])
def test_apply_vertex_deltas_moves_one_vertex_in_all_frames(name):
    b = open(os.path.join(MDL, name), "rb").read()
    before = parse_geometry(os.path.join(MDL, name), include_frames=True)
    vi = before["vertexIndices"][0]
    d = inward_grid_delta(before, vi)
    out = apply_vertex_deltas(b, {vi: d})

    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".mdl", delete=False) as f:
        f.write(out)
    after = parse_geometry(f.name, include_frames=True)
    os.unlink(f.name)

    moved = corners_of(before, vi)
    untouched = next(k for k, i in enumerate(before["vertexIndices"]) if i != vi)
    for fr_before, fr_after in zip(before["frames"], after["frames"]):
        for k in moved:
            for ax in range(3):
                assert fr_after[3 * k + ax] == pytest.approx(
                    fr_before[3 * k + ax] + d[ax], abs=1e-3)
        for ax in range(3):
            assert fr_after[3 * untouched + ax] == pytest.approx(
                fr_before[3 * untouched + ax], abs=1e-6)


def test_apply_vertex_deltas_clamps_to_bbox(tmp_path):
    src = os.path.join(MDL, "Paper2.MDL")
    b = open(src, "rb").read()
    g = parse_geometry(src)
    vi = g["vertexIndices"][0]
    a, bb = g["quant"]["a"], g["quant"]["b"]
    huge = [1e9 * (1 if a[ax] > 0 else -1) for ax in range(3)]  # push toward packed=max
    out = apply_vertex_deltas(b, {vi: huge})
    p = tmp_path / "clamped.mdl"
    p.write_bytes(out)
    after = parse_geometry(str(p))
    k = corners_of(g, vi)[0]
    for ax in range(3):
        assert after["positions"][3 * k + ax] == pytest.approx(
            a[ax] * 255 + bb[ax], abs=1e-3)


def test_apply_vertex_deltas_rejects_grouped_idpo():
    with pytest.raises(ValueError):
        apply_vertex_deltas(make_grouped_idpo(), {0: [1.0, 0.0, 0.0]})


def test_apply_vertex_deltas_rejects_out_of_range_index():
    b = open(os.path.join(MDL, "Paper2.MDL"), "rb").read()
    g = parse_geometry(os.path.join(MDL, "Paper2.MDL"))
    with pytest.raises(ValueError):
        apply_vertex_deltas(b, {g["numverts"]: [1.0, 0.0, 0.0]})
