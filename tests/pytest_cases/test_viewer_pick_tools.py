# SPDX-License-Identifier: Apache-2.0
"""Viewer pick tools without Houdini: Clay's material rewrite and the pixel readout text.

viewer/viewer_clay.py replaces a material's surface closure with a grey diffuse
BSDF and keeps volume and displacement; viewer/viewer_pick.py formats the
readout as one status-line part and reads float planes bottom-up.
"""

import importlib.util
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

VIEWER = Path(__file__).resolve().parents[2] / "houdini/python3.13libs/cycles_houdini/viewer"


def load(name, monkeypatch=None):
    spec = importlib.util.spec_from_file_location("viewer_pick_tools_" + name, VIEWER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    if monkeypatch is not None and "hou" not in sys.modules:
        # viewer_pick imports hou; the helpers tested here never call it. Removed after the test.
        monkeypatch.setitem(sys.modules, "hou", ModuleType("hou"))
    spec.loader.exec_module(module)
    return module


def node(identity, kind, **parameters):
    return dict(id=identity, definition=f"cycles/{kind}/1", parameters=parameters)


def link(source, output, target, socket):
    return {"from": dict(node=source, socket=output), "to": dict(node=target, socket=socket)}


def test_surface_becomes_clay_and_unused_surface_nodes_go():
    clay = load("viewer_clay")
    source = dict(
        schema_version=1,
        nodes=[node("texture", "image_texture", filename="a.exr"), node("shader", "principled_bsdf")],
        links=[link("texture", "color", "shader", "base_color")],
        surface=dict(node="shader", socket="BSDF"),
        settings=dict(transparent_shadows=False),
    )
    result = clay.graph(source)
    assert result["surface"] == dict(node=clay.NODE, socket="BSDF")
    assert [n["id"] for n in result["nodes"]] == [clay.NODE]
    assert result["nodes"][0]["definition"] == "cycles/diffuse_bsdf/1"
    assert result["nodes"][0]["parameters"]["color"] == clay.COLOR
    assert result["links"] == [] and result["settings"] == source["settings"]
    assert source["surface"] == dict(node="shader", socket="BSDF"), "The captured graph is not modified"


def test_volume_and_displacement_are_kept():
    clay = load("viewer_clay")
    source = dict(
        nodes=[
            node("shader", "principled_bsdf"),
            node("noise", "noise_texture"),
            node("bump", "displacement"),
            node("fog", "principled_volume"),
        ],
        links=[link("noise", "fac", "bump", "height"), link("noise", "color", "shader", "base_color")],
        surface=dict(node="shader", socket="BSDF"),
        displacement=dict(node="bump", socket="displacement"),
        displacement_method="both",
        volume=dict(node="fog", socket="volume"),
    )
    result = clay.graph(source)
    assert {n["id"] for n in result["nodes"]} == {clay.NODE, "noise", "bump", "fog"}
    assert result["links"] == [link("noise", "fac", "bump", "height")]
    assert result["displacement"] == source["displacement"] and result["volume"] == source["volume"]
    assert result["displacement_method"] == "both"
    volume_only = dict(nodes=[node("fog", "principled_volume")], links=[], volume=dict(node="fog", socket="volume"))
    assert clay.graph(volume_only) is volume_only


def test_fallback_shaders_and_resolver():
    clay = load("viewer_clay")
    surface = dict(key="rop:1/surface", kind="principled", color=(1, 0, 0), cryptomatte_name="/out/rop", pass_index=3)
    result = clay.material(surface)
    assert result == dict(
        key="rop:1/surface",
        kind="diffuse",
        color=clay.COLOR,
        roughness=0.0,
        cryptomatte_name="/out/rop",
        pass_index=3,
    )
    volume = dict(key="rop:1/surface", kind="principled_volume", density=2.0)
    assert clay.material(volume) is volume
    calls = []

    def resolve(references, at_time, shutter_times=None):
        calls.append((references, at_time))
        return [dict(key="native-material:a", kind="graph", graph=dict(nodes=[], links=[], surface=dict(node="x")))]

    update = dict(materials=[surface], material_resolver=resolve, passes=["combined"])
    applied = clay.apply(update)
    assert update["materials"] == [surface] and update["material_resolver"] is resolve
    assert applied["materials"][0]["kind"] == "diffuse" and applied["passes"] == ["combined"]
    resolved = applied["material_resolver"]([{"key": "native-material:a"}], 1.5)
    assert calls == [([{"key": "native-material:a"}], 1.5)]
    assert resolved[0]["graph"]["surface"]["node"] == clay.NODE
    assert clay.apply(applied)["material_resolver"] is applied["material_resolver"], "Never wrapped twice"
    camera_only = dict(camera_path="/obj/cam")
    assert clay.apply(camera_only) is camera_only


def test_readout_text_and_plane_values(monkeypatch):
    pick = load("viewer_pick", monkeypatch)
    text = pick.readout_text(4, 2, "Beauty", (0.5, 0.25, 0.125, 1.0), True, (188, 137, 99, 255), ("R", "G", "B", "A"))
    assert text == "x 4  y 2    Beauty linear R 0.5000  G 0.2500  B 0.1250  A 1.0000    display 188 137 99 255"
    assert " · " not in text, "The status headline keeps only the first ' · ' parts"
    assert pick.readout_text(0, 0, "Depth", (9.2,), False, None, ("Z",)) == "x 0  y 0    Depth Z 9.2000"
    assert pick.readout_text(1, 1, "Normal", (0.0, 1.0, 0.0), False, None) == (
        "x 1  y 1    Normal X 0.0000  Y 1.0000  Z 0.0000"
    )
    assert pick.readout_text(1, 1, "Snapshot 2", None, True, (1, 2, 3, 4)) == (
        "x 1  y 1    Snapshot 2 (no linear values)    display 1 2 3 4"
    )
    width, height = 3, 2
    pixels = struct.pack(f"{width * height * 2}f", *range(width * height * 2))
    frame = dict(x=10, y=20, width=width, height=height, planes=dict(uv=dict(channels=2, pixels=pixels)))
    assert pick.plane_value(frame, "uv", 11, 21) == (8.0, 9.0)
    assert pick.plane_value(frame, "uv", 9, 21) is None and pick.plane_value(frame, "depth", 11, 21) is None


def test_readout_of_odd_compact_frame_uses_logical_bottom_up_pixels(monkeypatch):
    qt = pytest.importorskip("PySide6.QtGui")
    pick = load("viewer_pick", monkeypatch)
    # Six distinct RGBA pixels, in top-down QImage order.
    data = bytes(range(24))
    image = qt.QImage(data, 3, 2, 12, qt.QImage.Format_RGBA8888)
    assert pick.display_value(image, 0, 0, (7, 5)) == (12, 13, 14, 15)
    assert pick.display_value(image, 2, 2, (7, 5)) == (12, 13, 14, 15)
    assert pick.display_value(image, 3, 3, (7, 5)) == (4, 5, 6, 7)
    assert pick.display_value(image, 6, 4, (7, 5)) == (8, 9, 10, 11)
    assert pick.display_value(image, 7, 4, (7, 5)) is None
