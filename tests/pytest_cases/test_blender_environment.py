# SPDX-License-Identifier: Apache-2.0
"""Every Blender process the bridge starts gets Blender's own OCIO configuration.

Houdini exports its ACES configuration as OCIO. Under it Blender has no "Non-Color" or
"sRGB": a library authored there fails or keeps ACES names, and a .blend opened and
saved from Houdini's "Open in Blender" would lose Blender's colour space names (live
finding, task L, 2026-11-13: tests/fixtures/blend_worlds/orientation_reference.py failed
on `colorspace_settings.name = "Non-Color"` when started from hython).
"""

import subprocess

from cycles_houdini.assets import providers
from cycles_houdini.assets.cache import IndexCache
from cycles_houdini.blender import blend_worker

HOUDINI = {
    "OCIO": "C:/Houdini/packages/ocio/houdini-config-v3.0.0_aces-v2.0_ocio-v2.5.ocio",
    "PYTHONPATH": "C:/Houdini/python313/lib",
    "PYTHONHOME": "C:/Houdini/python313",
    "PATH": "C:/Windows",
    "CYCLES_HOUDINI_BLENDER_OCIO": "",
}


def test_blender_environment_drops_houdini_python_and_ocio():
    env = blend_worker.blender_environment(base=HOUDINI)
    assert "OCIO" not in env and "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["PATH"] == "C:/Windows"
    assert blend_worker.blender_environment("D:/studio/blender.ocio", base=HOUDINI)["OCIO"] == "D:/studio/blender.ocio"
    # Windows spells variables in any case; none of them may slip through.
    assert "Ocio" not in blend_worker.blender_environment(base={"Ocio": "x"})


def test_worker_and_preference_dump_use_it(monkeypatch):
    for key, value in HOUDINI.items():
        monkeypatch.setenv(key, value)
    seen = {}

    def run(command, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"")

    monkeypatch.setattr(blend_worker.subprocess, "run", run)
    try:
        blend_worker.dump_asset_library_preferences("C:/Blender/blender.exe", timeout=10)
    except RuntimeError:
        pass
    assert "OCIO" not in seen["env"] and "PYTHONPATH" not in seen["env"]


def test_listing_generation_uses_blenders_configuration(tmp_path, monkeypatch):
    for key, value in HOUDINI.items():
        monkeypatch.setenv(key, value)
    seen = {}

    def run(command, **kwargs):
        seen["env"] = kwargs.get("env")
        (tmp_path / providers.META_FILENAME).write_text("{}")
        return type("Completed", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(providers.subprocess, "run", run)
    providers.BlenderListingProvider(str(tmp_path), IndexCache(str(tmp_path), root=tmp_path / "c")).generate_listing(
        "/bin/blender"
    )
    assert seen["env"] is not None and "OCIO" not in seen["env"] and "PYTHONHOME" not in seen["env"]
    monkeypatch.setenv("CYCLES_HOUDINI_BLENDER_OCIO", "D:/studio/blender.ocio")
    providers.BlenderListingProvider(str(tmp_path), IndexCache(str(tmp_path), root=tmp_path / "c")).generate_listing(
        "/bin/blender"
    )
    assert seen["env"]["OCIO"] == "D:/studio/blender.ocio"


def test_open_in_blender_uses_blenders_configuration(monkeypatch):
    from cycles_houdini.assets import panel

    for key, value in HOUDINI.items():
        monkeypatch.setenv(key, value)
    seen = {}

    def popen(command, **kwargs):
        seen["command"], seen["env"] = command, kwargs.get("env")

    monkeypatch.setattr(panel.subprocess, "Popen", popen)
    panel.CoreBackend().launch_blender("/bin/blender", "/library/props.blend")
    assert seen["command"] == ["/bin/blender", "/library/props.blend"]
    assert seen["env"] is not None and "OCIO" not in seen["env"] and "PYTHONPATH" not in seen["env"]
