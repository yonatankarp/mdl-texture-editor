import os
import pytest
from mdl_geometry import parse_geometry

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
