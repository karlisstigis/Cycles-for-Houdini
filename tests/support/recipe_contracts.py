# SPDX-License-Identifier: Apache-2.0
"""Numerical contracts for source-audited recipes.

The scripts retain native/worker ownership assertions inside fresh processes.
These contracts independently check their exported numerical/domain evidence.
They run on the existing nightly executions and on opt-in recipe pytest items.
"""

import math

DEPENDENT_RECIPES = {
    "houdini_panel_motion_timing_smoke.py": "panel shutter coverage and retained geometry",
    "houdini_transparency_batch_smoke.py": "animated surface and emissive-volume RGBA",
    "houdini_queued_worker_smoke.py": "worker ownership, coalescing and recovery",
    "houdini_texture_cache_smoke.py": "texture revisions and isolated cache derivatives",
    "houdini_texture_normal_smoke.py": "analytic normal and bump vectors",
    "houdini_texture_auto_batch_smoke.py": "animated Auto color-space conversion",
    "core_strand_material_smoke.py": "independent point and curve palette images",
    "core_volume_material_smoke.py": "independent absorption/emission/blackbody images",
    "core_displacement_smoke.py": "independent displacement RGBA and data planes",
    "core_subdivision_smoke.py": "independent Catmull-Clark RGBA and interior data",
}
AUDITED_RECIPES = {**DEPENDENT_RECIPES, "houdini_light_parenting_smoke.py": "native light hierarchy transforms"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def bounded(value, upper, label, *, inclusive=False):
    require(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value),
        label + " is not finite",
    )
    require(0 <= value <= upper if inclusive else 0 <= value < upper, label + " exceeds its independent tolerance")


def near(actual, expected, tolerance, label):
    require(len(actual) == len(expected), label + " has an incorrect vector shape")
    for value, target in zip(actual, expected, strict=True):
        bounded(abs(value - target), tolerance, label, inclusive=True)


def pairs(rows, fields, expected, label):
    require({tuple(row[field] for field in fields) for row in rows} == set(expected), label + " is incomplete")
    require(len(rows) == len(expected), label + " repeats a case")


def audit_recipe(test, report):
    if test not in AUDITED_RECIPES:
        return None
    require(report.get("result") == "passed", "Dependent recipe did not pass")
    if test == "houdini_panel_motion_timing_smoke.py":
        expected = [0.32, 0.32, 0.32, 0.64, 0.8, 0.64, 0.64, 0.8, 0.64, 0.8, 0.64, 0.8, 0.64]
        checks = report["checks"]
        require(len(checks) == len(expected), "Panel shutter cases are incomplete")
        for index, (row, red) in enumerate(zip(checks, expected, strict=True)):
            near([row["measured_red"]], [red], 0.045, "Panel analytic coverage")
            require(row["expected_red"] == red, "Panel expected coverage changed")
            require(row["status"]["state"] == "complete", "Panel generation did not complete")
            if index:
                require(
                    row["status"]["renderer_pid"] == checks[index - 1]["status"]["renderer_pid"], "Panel worker changed"
                )
            if index in (1, 2):
                require(
                    row["status"]["geometry_conversions"] == checks[0]["status"]["geometry_conversions"],
                    "Camera move recaptured geometry",
                )
    elif test == "houdini_light_parenting_smoke.py":
        checks = report["cases"]
        hierarchy = (
            "nested rotated parent",
            "nonuniform parent",
            "mirrored parent",
            "reparent",
            "disconnect parent",
            "restore parent",
        )
        shapes = ("rectangle", "ellipse", "square", "disk")
        look_at = "Look At under an animated mirrored nonuniform parent"
        pairs(
            checks,
            ("case", "frame"),
            [(label, 1) for label in hierarchy]
            + [(look_at, frame) for frame in (1, 5, 10)]
            + [("native " + shape + " area size and aim", 10) for shape in shapes],
            "Native light pose cases",
        )
        for row in checks:
            bounded(row["max_matrix_error"], 1e-8, "Native light transform")
            require(len(row["pose"]) == 12, "Native light pose must have three rows of an affine transform")
            near(row["pose"], row["stock_pose"], 1e-8, "Independent stock light matrix")
        require(
            len({tuple(row["pose"]) for row in checks if row["case"] == look_at}) == 3,
            "Animated Look At did not change the pose",
        )
    elif test == "houdini_texture_cache_smoke.py":
        cases = report["cases"]
        require(len(cases) == 6, "Cache routes must cover both internally selected devices")
        for offset in (0, 3):
            pairs(
                cases[offset : offset + 3],
                ("cache", "automatic_generation"),
                [(False, False), (True, True), (True, False)],
                "Cache routes",
            )
        for row in cases:
            near(row["initial"], [0.2, 0.4, 0.8], 0.001, "Initial texture color")
            near(row["updated"], [0.7, 0.3, 1.2], 0.001, "Revised texture color")
            require(bool(row["tx_files"]) == row["automatic_generation"], "Cache derivative generation route changed")
            require(all(file["bytes"] > 0 for file in row["tx_files"]), "Empty cache derivative")
    elif test == "houdini_texture_normal_smoke.py":
        devices = report["devices"]
        require(len(devices) == 2, "Normal maps must cover both internally selected devices")
        slope = 0.125 / math.sqrt(1 + 0.125**2)
        z = 1 / math.sqrt(1 + 0.125**2)
        expected = [(0, 0.5, math.sqrt(0.75)), (0, -0.5, math.sqrt(0.75)), (1, 0, 0), (-slope, 0, z), (slope, 0, z)]
        analytic_names = [
            "OpenGL tangent normal in the independently defined UV basis",
            "DirectX convention reverses the tangent green channel",
            "World normal follows the specified world X axis",
            "Texture bump agrees with height derivative du/dx = 1/8",
            "Inverted bump reverses the analytic slope",
        ]
        for row in devices:
            checks = row["checks"]
            require(row["result"] == "passed" and len(checks) == 11, "Normal/bump cases are incomplete")
            require(
                [check["name"] for check in checks[:5]] == analytic_names, "Analytic normal/bump categories changed"
            )
            for check, target in zip(checks[:5], expected, strict=True):
                near(
                    check["measured"],
                    [value * 0.5 + 0.5 for value in target],
                    0.001,
                    "Analytic tangent/world/bump normal",
                )
            static = checks[5:9]
            require(
                all(check["name"] == "Static corner normals survive a neutral tangent normal map" for check in static),
                "Static corner-normal category changed",
            )
            require(
                {(check["basis"], tuple(check["authored_normal"])) for check in static}
                == {
                    (basis, normal)
                    for basis in ("original", "displaced")
                    for normal in ((0, 0, 1), (0.3, 0.4, math.sqrt(0.75)))
                },
                "Static corner-normal bases or authored vectors are incomplete",
            )
            for check in static:
                near(
                    check["measured"],
                    [value * 0.5 + 0.5 for value in check["authored_normal"]],
                    0.0001,
                    "Static corner-normal preservation",
                )
            moving = checks[9:]
            require(
                all(
                    check["name"] == "Moving corner normals preserve ray-time shading with a neutral map"
                    for check in moving
                ),
                "Moving corner-normal category changed",
            )
            pairs(moving, ("basis",), [("original",), ("displaced",)], "Moving corner-normal bases")
            for check in moving:
                require(len(check["reference"]) == 3, "Moving reference normal has an incorrect vector shape")
                for component in check["reference"]:
                    bounded(component, 1, "Encoded moving reference normal", inclusive=True)
                near(check["reference"], moving[0]["reference"], 1e-7, "Shared geometric ray-time reference")
                near(check["measured"], check["reference"], 0.0001, "Moving corner-normal preservation")
    elif test == "houdini_texture_auto_batch_smoke.py":
        pairs(report["cases"], ("cache",), [(False,), (True,)], "Auto cache routes")
        for row in report["cases"]:
            require([frame["frame"] for frame in row["frames"]] == [1, 2], "Auto sequence frames changed")
            require(
                [frame["input_space"] for frame in row["frames"]]
                == ["Linear Rec.709 (sRGB)", "sRGB Encoded Rec.709 (sRGB)"],
                "Animated Auto rule did not switch",
            )
            for frame in row["frames"]:
                near(frame["measured"], frame["expected"], 0.001, "Auto working-space conversion")
        require(report["diagnostic"] != report["repaired"], "Missing-input diagnostic was not repaired independently")
    elif test == "houdini_transparency_batch_smoke.py":
        cases = report["cases"]
        require(len(cases) == 4, "Transparency must cover surface and emission on both devices")
        for offset in (0, 2):
            pairs(cases[offset : offset + 2], ("geometry",), [("surface",), ("emission",)], "Transparency geometry")
        for row in cases:
            require([frame["frame"] for frame in row["frames"]] == [1, 2], "Transparency sequence frames changed")
            for frame in row["frames"]:
                transparent = frame["frame"] == 1
                require(frame["transparent"] is transparent, "Animated film coverage did not change")
                near(
                    frame["background_rgba"],
                    [0, 0, 0, 0] if transparent else [0.05, 0.1, 0.15, 1],
                    2e-5,
                    "Film background RGBA",
                )
                expected = (
                    [0.4, 0.2, 0.1, 1]
                    if row["geometry"] == "surface"
                    else [0.5 * 9 / 8, 0.25 * 9 / 8, 0.125 * 9 / 8, 0 if transparent else 1]
                )
                if row["geometry"] == "emission" and not transparent:
                    expected[:3] = [a + b for a, b in zip(expected[:3], (0.05, 0.1, 0.15), strict=True)]
                near(
                    frame["foreground_rgba"],
                    expected,
                    2e-5 if row["geometry"] == "surface" else 0.01,
                    "Surface/emissive volume RGBA",
                )
    elif test == "houdini_queued_worker_smoke.py":
        images = report["images"]
        require(set(images) == {"sync", "worker"}, "Worker comparison modes changed")
        for row in images.values():
            for field, value in dict(
                geometry_resources=5, packed_instances=54, points=360, curves=265, displacement_rebuilds=1
            ).items():
                require(row["status"][field] == value, "Courtyard capture counts changed: " + field)
        bounded(images["worker"]["baseline_rmse"], 0.005, "Worker/sync RGBA RMSE")
        checks = {row["name"]: row for row in report["cases"]}
        coalesced = checks["paused update and lossless coalescing"]
        bounded(coalesced["rmse"], 0.005, "Lossless coalescing RMSE")
        require(coalesced["after"]["coalesced_updates"] >= 1, "No update was coalesced")
        require(
            coalesced["after"]["geometry_conversions"] > coalesced["before"]["geometry_conversions"],
            "Coalescing lost geometry edit",
        )
        recovery = checks["forced renderer termination and recovery"]
        require(
            recovery["failure"]["error"] and recovery["recovered"]["backend_epoch"] == 2, "Owned worker did not recover"
        )
        pending = checks["immediate Stop -> Render retains the new generation"]["pending"]
        require(
            pending["state"] == "rendering"
            and pending["pending_update"]
            and pending["committed_generation"] < pending["generation"],
            "Restart generation was lost",
        )
        stopped = checks["Stop discards pending edits; restart captures the current scene"]["stopped"]
        require(stopped["state"] == "cancelled" and not stopped["pending_update"], "Stop retained a discarded edit")
    else:
        reports = report["reports"]
        require(len(reports) == 2, "Independent core images must cover both internally selected devices")
        checks = {
            "core_strand_material_smoke.py": 30,
            "core_volume_material_smoke.py": 15,
            "core_displacement_smoke.py": 10,
            "core_subdivision_smoke.py": 8,
        }[test]
        for row in reports:
            require(
                row["events"][-1]["result"] == "passed" and row["events"][-1]["checks"] == checks,
                "Independent harness checks changed",
            )
            if test == "core_subdivision_smoke.py":
                bounded(row["maximum_absolute_rgba_difference"], 0.1 + 1e-6, "Subdivision RGBA maximum", inclusive=True)
                bounded(row["mean_absolute_rgba_difference"], 0.002, "Subdivision RGBA mean")
                require(
                    set(row["interior_data"]) == {"depth.Z", "position.Z", "normal.Z"},
                    "Subdivision data planes are incomplete",
                )
                for field, tolerance in (("depth.Z", 0.01), ("position.Z", 0.01), ("normal.Z", 0.08)):
                    bounded(
                        row["interior_data"][field]["maximum_absolute_difference"], tolerance, "Subdivision " + field
                    )
                continue
            kinds = {
                "core_strand_material_smoke.py": {"points", "curves"},
                "core_volume_material_smoke.py": {"absorption", "emission", "blackbody"},
                "core_displacement_smoke.py": {"plane", "seams"},
            }[test]
            require(
                {image["kind"] for image in row["images"]} == kinds and len(row["images"]) == len(kinds),
                "Independent image set is incomplete",
            )
            for image in row["images"]:
                if test == "core_strand_material_smoke.py":
                    bounded(image["maximum_absolute_rgba_difference"], 0.001, "Strand palette RGBA")
                elif test == "core_volume_material_smoke.py":
                    tolerance = 0.1 if image["kind"] == "blackbody" else 0.025
                    bounded(image["maximum_absolute_rgba_difference"], tolerance, "Volume " + image["kind"])
                    require(bool(image["working_space"]), "Volume image lacks its working space")
                else:
                    bounded(image["maximum_absolute_rgba_difference"], 0.025, "Displacement RGBA maximum")
                    bounded(image["mean_absolute_rgba_difference"], 0.001, "Displacement RGBA mean")
                    require(
                        set(image["interior_data_differences"]) == {"depth.Z", "position.Z", "normal.Z"},
                        "Displacement data planes are incomplete",
                    )
                    for value in image["interior_data_differences"].values():
                        bounded(value, 0.001, "Displacement interior data")
    return dict(result="passed", domain=AUDITED_RECIPES[test])
