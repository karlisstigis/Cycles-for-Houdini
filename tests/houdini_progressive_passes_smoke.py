# SPDX-License-Identifier: Apache-2.0
"""Select raw/light passes without sample resets, redundant display or all-plane reads."""

from support.paths import artifacts_root
import json
import os
from pathlib import Path
import sys
import time
import traceback
import cycles_houdini as ch
from cycles_houdini.passes import catalogue

destination = artifacts_root() / "stage3/passes"
destination.mkdir(parents=True, exist_ok=True)
report_path = destination / "houdini-progressive-passes-smoke.json"
names = [p["id"] for p in catalogue() if not p["managed"]]
devices = (sys.argv[sys.argv.index("--device") + 1],) if "--device" in sys.argv else ("CPU", ch.default_device())
report = dict(result="running", execution=os.environ.get("CYCLES_HOUDINI_EXECUTION", "sync"), devices=[])


def wait(session, generation, expected, after=0):
    began = time.monotonic()
    while time.monotonic() - began < 30:
        status = session.status()
        if status["state"] == "failed":
            raise RuntimeError(status["error"])
        frame = session.frame_info(after)
        if frame and frame["generation"] == generation and set(frame["passes"]) == set(expected):
            assert not frame["complete"] and status["progress"] < 1
            return frame, status
        time.sleep(0.005)
    raise TimeoutError((expected, session.frame_info(), session.status()))


try:
    for device in devices:
        checks = []
        with ch.Session(
            device=device, width=160, height=90, samples=1048576, passes=names, collect_diagnostics=True
        ) as session:
            generation = session.calibration()
            info, before = wait(session, generation, ["combined"])
            assert set(info["enabled_passes"]) == set(names)
            assert not session.diagnostics()["output_plane_reads"], "Beauty-only display read all float buffers"
            for name in ("normal", "diffuse_direct", "depth", "roughness", "volume_direct", "sample_count"):
                began = time.monotonic()
                session.set_preview_pass(name)
                # Requested raw beauty is retained for instant noisy/denoised comparison.
                selected = ["combined", "noisy_combined", name]
                info, status = wait(session, generation, selected, info["sequence"])
                selection_latency = time.monotonic() - began
                assert status["generation"] == generation
                assert status["geometry_conversions"] == before["geometry_conversions"]
                assert status["current_sample"] >= before["current_sample"]
                diagnostics = session.diagnostics()
                # No half-float DisplayDriver copy after float output owns the image.
                deliveries = diagnostics["output_deliveries"]
                display_updates = diagnostics["display_updates"]
                following, _ = wait(session, generation, selected, info["sequence"])
                following_diagnostics = session.diagnostics()
                assert following_diagnostics["display_updates"] == display_updates
                if name in ("normal", "depth", "roughness", "sample_count"):
                    # Both conversions use the retained immutable frame. Pause
                    # for the identity check so another update cannot race them.
                    session.pause()
                    deadline = time.monotonic() + 10
                    while session.status()["state"] != "paused" and time.monotonic() < deadline:
                        time.sleep(0.005)
                    assert session.status()["state"] == "paused"
                    image = session.display_image(**{"pass": name}, exposure=0)
                    same = session.display_image(**{"pass": name}, exposure=3, display="invalid", view="invalid")
                    assert image["rgba"] == same["rgba"], "Data passes received color/exposure processing"
                    session.pause(False)
                checks.append(
                    dict(
                        pass_id=name,
                        select_to_image_seconds=selection_latency,
                        generation=generation,
                        geometry_conversions=status["geometry_conversions"],
                        samples=status["current_sample"],
                        output_deliveries=deliveries,
                        output_plane_reads=following_diagnostics["output_plane_reads"],
                    )
                )
                info, before = following, status
            # No unnecessary planes were converted during individual inspection.
            assert not {"uv", "position", "emission", "ao"} & set(session.diagnostics()["output_plane_reads"])
            session.open_native_display("null:")
            info, status = wait(session, generation, names, info["sequence"])
            session.present_native()
            session.close_native_display()
            info, status = wait(session, generation, ["combined", "noisy_combined", "sample_count"], info["sequence"])
            assert status["generation"] == generation
            checks.append(
                dict(
                    name="Native multi-plane output switches back to selected-only output without restarting",
                    status=status,
                )
            )
            session.cancel()
            deadline = time.monotonic() + 10
            while session.status()["state"] != "cancelled" and time.monotonic() < deadline:
                time.sleep(0.005)
            assert session.status()["state"] == "cancelled"
            sequence = session.frame_info()["sequence"]
            time.sleep(0.06)
            assert session.frame_info()["sequence"] == sequence
        assert session.status()["owned_geometry_bytes"] == session.status()["device_allocation_bytes"] == 0
        report["devices"].append(dict(device=device, checks=checks))
    report["result"] = "passed"
except BaseException:
    report.update(result="failed", error=traceback.format_exc())
    raise
finally:
    report_path.write_text(json.dumps(report, indent=2))
    print(str(report_path))
