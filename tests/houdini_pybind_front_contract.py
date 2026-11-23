# SPDX-License-Identifier: Apache-2.0
"""Argument, metadata and frame ownership contracts against the real native DSO."""

import argparse
import gc
import hashlib
import json
import threading
from pathlib import Path

import hou
import numpy as np
import cycles_houdini as ch

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--device", choices=("CPU",), default="CPU", help="This argument/ownership contract is host-only")
parser.add_argument("--report", type=Path)
args = parser.parse_args()
native = ch._native
checks = 0


def rejects(kind, call, message=None):
    global checks
    try:
        call()
    except Exception as error:
        assert type(error) is kind, (type(error), kind, str(error))
        if message is not None:
            assert message in str(error), (message, str(error))
    else:
        raise AssertionError("Expected " + kind.__name__)
    checks += 1


# The established entry points accept positional arguments and preserve scalar
# parser failures separately from native settings/lifecycle validation.
rejects(TypeError, lambda: native.request_thumbnail())
rejects(TypeError, lambda: native.thumbnail_pixels(0, 1))
rejects(TypeError, lambda: native.thumbnail_pixels(node=0))
rejects(TypeError, lambda: native.thumbnail_pixels(0.5))
rejects(OverflowError, lambda: native.thumbnail_pixels(2**80))
rejects(OverflowError, lambda: native.request_thumbnail(0, 2**40))
rejects(TypeError, lambda: native.initialize(b"wrong type"))


class Indexed:
    def __index__(self):
        return 3


# Signed parser fields support __index__; unsigned K fields require actual
# integers, mask negative/large values, and then reach native handle checks.
assert native.thumbnail_pixels(Indexed()) == native.thumbnail_pixels(3)
rejects(TypeError, lambda: native.owned_frame(object(), Indexed()))
rejects(TypeError, lambda: native.present_frame(object(), object(), Indexed()))
rejects(RuntimeError, lambda: native.owned_frame(object(), -1), "session handle")
rejects(RuntimeError, lambda: native.owned_frame(object(), 2**100), "session handle")

rejects(RuntimeError, lambda: native.fixture(object()))
rejects(RuntimeError, lambda: native.blend_mesh_nbytes(object()))
rejects(RuntimeError, lambda: native.current_thumbnail((0, -1, 128)))
rejects(RuntimeError, lambda: native.current_thumbnail((0, 0, 31)))
rejects(RuntimeError, lambda: native.current_thumbnail([0, 0]))
assert native.current_thumbnail((0, 0, 128)) is False
assert native.publish_thumbnail((0, 0, 128), b"") is False
# Empty multidimensional buffers remain contiguous even with zero strides.
assert native.publish_thumbnail((0, 0, 128), np.empty((0, 3), dtype=np.float32)) is False

assert isinstance(native.take_thumbnails(), list)
stats = native.thumbnail_stats()
assert (stats["queue_limit"], stats["image_limit"], stats["byte_limit"]) == (16, 32, 8 * 1024 * 1024)

valid = {
    "materials": [{"pass_index": 32767}],
    "geometry": [{"object_properties": {"pass_index": 0, "dielectric_priority": -16777216}}],
}
assert native.validate_update_metadata(valid) is None
for index in (True, -1, 32768, 2**100, 1.5):
    rejects(RuntimeError, lambda index=index: native.validate_update_metadata({"materials": [{"pass_index": index}]}))
for priority in (True, 16777217, -(2**100), "0"):
    rejects(
        RuntimeError,
        lambda priority=priority: native.validate_update_metadata(
            {"geometry": [{"object_properties": {"dielectric_priority": priority}}]}
        ),
    )
assert native.validate_aovs([{"name": "custom", "type": "color"}]) is None
assert native.validate_lightgroups(("key", "fill")) is None


class IndexedList(list):
    def __iter__(self):
        raise AssertionError("Native list capture must use indexed list storage")


assert native.validate_lightgroups(IndexedList(["key", "fill"])) is None


class StorageDict(dict):
    def __getitem__(self, key):
        raise AssertionError("Native dictionary capture must use stored values")


assert native.validate_update_metadata(StorageDict(materials=[StorageDict(pass_index=1)])) is None
assert native.validate_update_metadata({}) is None
rejects(RuntimeError, lambda: native.validate_update_metadata({"cryptomatte": None}), "settings dictionary")
rejects(RuntimeError, lambda: native.validate_aovs([{"name": "field"}]), "Missing field: type")
rejects(RuntimeError, lambda: native.validate_aovs([{"name": "field", "type": None}]), "string identifiers")

rejects(RuntimeError, lambda: native.validate_aovs([{"name": "bad\0name", "type": "color"}]))
rejects(RuntimeError, lambda: native.validate_aovs([{"name": "bad", "type": "color", "extra": 1}]))
rejects(RuntimeError, lambda: native.validate_lightgroups("key"))

rejects(RuntimeError, lambda: native.shader_curve_table([2**100], "float"), "signed 32-bit range")

# Python numeric conversion can mutate its source container. A converter must
# retain every input item until the complete typed value has been captured.
identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]
converted = []
transform = []


class Component:
    def __init__(self, index, value):
        self.index, self.value = index, value

    def __float__(self):
        converted.append(self.index)
        if self.index == 0:
            transform.clear()
            gc.collect()
        return self.value


transform.extend(Component(index, value) for index, value in enumerate(identity))
camera = native.resolve_camera_motion(
    dict(projection_code=0, transform=transform, fov=0.7, near=0.1, far=100.0, ortho_scale=1.0),
    [],
    0.0,
    "binding ownership fixture",
)
assert not transform and converted == list(range(12)), converted
assert camera["transform"] == identity, camera

# A nested dictionary value must survive a numeric callback that removes its
# sole parent reference before later fields from that same value are consumed.
obj = hou.node("/obj").createNode("geo", "pybind_owned_dictionary", run_init_scripts=False)
grid = obj.createNode("grid")
grid.setParms(dict(type=0, rows=2, cols=2))
source_camera = hou.node("/obj").createNode("cam", "pybind_owned_dictionary_camera")
request = ch.geometry_request(obj, grid, material="owned_dictionary")
obj.addSpareParmTuple(hou.FloatParmTemplate("binding_gain", "Binding Gain", 1))
obj.parm("binding_gain").setExpression("$F")
stylesheet = json.dumps(
    {
        "styles": [
            {
                "target": {"type": "self"},
                "overrides": {
                    "materialParameters": {
                        "power": {
                            "script": {
                                "code": 'cvex binding_clock(export float result=0){result=chf("binding_gain");}',
                                "return": "result",
                            }
                        }
                    }
                },
            }
        ]
    }
)
previous_frame = hou.frame()
try:
    for frame_number in (1, 3, 7):
        hou.setFrame(frame_number)
        implicit = native.object_stylesheet(obj.path(), [], stylesheet, "")
        explicit = native.object_stylesheet(obj.path(), [], stylesheet, "", hou.time())
        assert implicit == explicit and not implicit["errors"], (implicit, explicit)
        power = json.loads(implicit["json"])["styles"][0]["overrides"]["materialParameters"]["power"]
        if isinstance(power, dict):
            power = power["value"]
        assert power == frame_number, (frame_number, power)
finally:
    hou.setFrame(previous_frame)


class RemovingRate:
    def __float__(self):
        request.pop("subdivision")
        gc.collect()
        return 1.0


request["subdivision"] = dict(type="linear", space="object", dicing_rate=RemovingRate(), max_level=1)
capture = native.create("CPU", 16, 16, 1, False, False, True, False, None, True)
try:
    snapshot = native.apply(
        capture,
        dict(
            geometry=[request],
            camera_path=source_camera.path(),
            materials=[dict(key="owned_dictionary", kind="emission", color=(1.0, 1.0, 1.0))],
        ),
        hou.time(),
    )
    assert "subdivision" not in request
    assert native.snapshot_info(snapshot)["mesh_vertices"] >= 4
finally:
    native.close(capture)
    obj.destroy()
    source_camera.destroy()

# The byte-format buffer owner remains immutable and holds the frame after
# its source capsule is retired, including final consumer release off-thread.
frame = native.compact_frame_fixture()
copied = native.display_gpu_input(frame, {})
borrowed = native.display_gpu_input(frame, {}, "", False)
assert copied["pixels_copied"] is True and type(copied["pixels"]) is bytes
assert borrowed["pixels_copied"] is False
view = borrowed["pixels"]
assert type(view) is memoryview and type(view.obj).__name__ == "FramePixels"
assert view.readonly and view.c_contiguous and view.format == "B" and view.ndim == 1
assert view.nbytes == 3 * 2 * 4 * 4
expected = hashlib.sha256(copied["pixels"]).hexdigest()
assert hashlib.sha256(view).hexdigest() == expected
rejects(TypeError, lambda value=view: value.__setitem__(0, 0))
rejects(BufferError, lambda value=view: native.publish_thumbnail((0, 0, 128), value[::2]))
del frame, copied, borrowed
gc.collect()
assert hashlib.sha256(view).hexdigest() == expected
retained = [view]
del view
errors = []


def release_consumer():
    try:
        assert hashlib.sha256(retained[0]).hexdigest() == expected
        retained.clear()
        gc.collect()
    except BaseException as error:
        errors.append(repr(error))


thread = threading.Thread(target=release_consumer)
thread.start()
thread.join(10)
assert not thread.is_alive() and not errors and not retained, errors
report = dict(
    result="passed",
    scope="Host-only arguments, metadata, capture and owned frame bytes",
    device=args.device,
    negative_cases=checks,
    owned_frame_bytes=True,
    off_thread_release=True,
)
if args.report:
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
