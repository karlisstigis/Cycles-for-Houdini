# SPDX-License-Identifier: Apache-2.0
"""Automatic separate EXR paths retain sequence fields and explicit routing."""

from support.package_loader import load_module
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest

package = ModuleType("sequence_output_test_host")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini")]
sys.modules[package.__name__] = package
controls = ModuleType(package.__name__ + ".rop_controls")
controls.value = lambda rop, name, now: rop.settings[name]
sys.modules[controls.__name__] = controls
hou = ModuleType("hou")
hou.OperationFailed = type("OperationFailed", (Exception,), {})
hou.timeToFrame = lambda value: value


def expand_frame(token, frame):
    token = token.replace("$", "").strip("{}")
    if token == "FF":
        return str(frame)
    return f"{int(frame):0{int(token[1:] or 0)}}"


hou.text = SimpleNamespace(expandStringAtFrame=expand_frame)
sys.modules["hou"] = hou
output = load_module(package, "output")
passes = ModuleType(package.__name__ + ".rop.passes")
passes.entries = lambda rop, now, output=False: [
    dict(
        row,
        separate=(row["separate"] or rop.settings["output_separate_passes"])
        and not (rop.settings["output_multipart"] and not rop.settings["output_separate_passes"]),
    )
    for row in rop.rows
]
sys.modules[passes.__name__] = passes
sys.modules[package.__name__ + ".rop"].passes = passes


class Rop:
    def __init__(self, raw, evaluated, *, folders=False, separate=True, multipart=False, route="", frame=1):
        self.raw = raw
        self.frame = frame
        self.settings = dict(
            picture=evaluated,
            output_multipart=multipart,
            output_separate_passes=separate,
            output_pass_folders=folders,
            output_precision="half",
            output_compression="zip",
            output_dwa_level=45,
            crop_to_region=False,
            pixel_aspect=1,
            output_color_override=False,
        )
        self.rows = [
            dict(
                type="ao", name="ao", precision="float", separate=False, path=route, compression="inherit", dwa_level=45
            )
        ]

    def parm(self, name):
        def raw():
            if isinstance(self.raw, Exception):
                raise self.raw
            return self.raw

        return SimpleNamespace(unexpandedString=raw)


class SequenceOutputTests(unittest.TestCase):
    def paths(self, rop, filename=None):
        return [Path(job["filename"]) for job in output.plan(rop, rop.frame, filename)]

    def test_default_frame_name_and_pass_folders(self):
        for folders in (False, True):
            rop = Rop(
                "render/Blender_Instancing.cam_2.$F4.exr", "render/Blender_Instancing.cam_2.0001.exr", folders=folders
            )
            beauty, ao = self.paths(rop)
            self.assertEqual(beauty.name, "Blender_Instancing.cam_2.0001.exr")
            self.assertEqual(ao.name, "Blender_Instancing.cam_2.ao.0001.exr")
            self.assertEqual(ao.parent.name, "ao" if folders else "render")
            self.assertEqual(beauty.parent.name, "beauty" if folders else "render")

    def test_frame_padding_braces_negative_and_repeated_numbers(self):
        for raw, evaluated, expected in (
            ("shot.$F.exr", "shot.1.exr", "shot.ao.1.exr"),
            ("shot2$F4.exr", "shot20001.exr", "shot2.ao.0001.exr"),
            ("$F4.exr", "0001.exr", "ao.0001.exr"),
            ("shot.${F4}.exr", "shot.0001.exr", "shot.ao.0001.exr"),
            ("shot.$F4.exr", "shot.-001.exr", "shot.ao.-001.exr"),
            ("shot.$F4.exr", "shot.10000.exr", "shot.ao.10000.exr"),
            ("shot_0001.$F4.exr", "shot_0001.0001.exr", "shot_0001.ao.0001.exr"),
            ("shot.$F4.$F4.exr", "shot.0001.0001.exr", "shot.0001.ao.0001.exr"),
            ("shot.$F4.0001.exr", "shot.0001.0001.exr", "shot.ao.0001.0001.exr"),
            ("shot_$F4.exr", "shot_0001.exr", "shot.ao_0001.exr"),
            ("shot-$F4.exr", "shot-0001.exr", "shot.ao-0001.exr"),
            ("shot-$F4.exr", "shot--001.exr", "shot.ao--001.exr"),
            ("shot.2.$FF.exr", "shot.2.1.exr", "shot.2.ao.1.exr"),
            ("shot.2.$FF.exr", "shot.2.1.25.exr", "shot.2.ao.1.25.exr"),
            ("shot.$FF.exr", "shot.1.25.exr", "shot.ao.1.25.exr"),
        ):
            with self.subTest(raw=raw):
                frame = (
                    -1 if "-001" in evaluated else 10000 if "10000" in evaluated else 1.25 if "1.25" in evaluated else 1
                )
                self.assertEqual(self.paths(Rop(raw, evaluated, frame=frame))[1].name, expected)

    def test_static_numbers_and_directory_frame_tokens_stay_literal(self):
        for raw, evaluated in (
            ("shot.cam_2.exr", "shot.cam_2.exr"),
            ("shot.0001.exr", "shot.0001.exr"),
            ("$HIP/0001/shot.cam_2.exr", "render/0001/shot.cam_2.exr"),
            ("$HIP/$F4/shot.0001.exr", "render/0001/shot.0001.exr"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(self.paths(Rop(raw, evaluated))[1].name, Path(evaluated).stem + ".ao.exr")

    def test_complex_expressions_and_filename_overrides_use_static_fallback(self):
        for raw in ("shot.`padzero(4,$F)`.exr", hou.OperationFailed("expression")):
            self.assertEqual(self.paths(Rop(raw, "shot.0001.exr"))[1].name, "shot.0001.ao.exr")
        rop = Rop("shot.$F4.exr", "shot.0001.exr")
        self.assertEqual(self.paths(rop, "chosen.0001.exr")[1].name, "chosen.0001.ao.exr")

    def test_explicit_routes_per_row_and_multilayer_unchanged(self):
        rop = Rop("shot.$F4.exr", "shot.0001.exr", folders=True, route="chosen/ao.0001.exr")
        self.assertEqual(self.paths(rop)[1], Path("chosen/ao.0001.exr").absolute())
        rop = Rop("shot.$F4.exr", "shot.0001.exr", separate=False)
        self.assertEqual(len(self.paths(rop)), 1)
        rop.rows[0]["separate"] = True
        self.assertEqual(self.paths(rop)[1].name, "shot.ao.0001.exr")
        rop.settings["output_multipart"] = True
        self.assertEqual(len(self.paths(rop)), 1)
        rop.settings["output_separate_passes"] = True
        self.assertEqual(self.paths(rop)[1].name, "shot.ao.0001.exr")


if __name__ == "__main__":
    unittest.main()
