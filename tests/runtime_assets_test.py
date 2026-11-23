# SPDX-License-Identifier: Apache-2.0
"""Immutable staging rejects changed inputs/assets and never rewrites reuse."""

import importlib.util
import json
from pathlib import Path
import uuid
from support.paths import artifacts_root

root = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runtime_assets", root / "scripts/runtime-assets.py")
assets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(assets)
destination = artifacts_root() / "stage0/runtime-assets" / uuid.uuid4().hex
assert destination.resolve().is_relative_to(artifacts_root().resolve())
destination.mkdir(parents=True)
checks = []


def fixture(name):
    project = destination / name
    host = project / "build/windows-h22-release/hosts/test"
    for relative in (
        "CMakeLists.txt",
        "CMakePresets.json",
        "source.lock.json",
        "scripts/stage-development.ps1",
        "scripts/runtime-assets.py",
        "scripts/build_layout.py",
        "scripts/build-layout.ps1",
        "scripts/create-light-asset.py",
        "scripts/create-blender-assets.py",
        "scripts/sdk-dependencies.py",
        "scripts/environment-report.py",
        "scripts/engine_source.py",
        "scripts/generate-node-help.py",
        "scripts/generate-property-help.py",
        "scripts/generate-viewer-help-icons.py",
        "scripts/pass-schema.py",
        "scripts/finalize-assets.py",
        "scripts/offline-scripts.py",
        "scripts/rop-layout.py",
        "scripts/rop-layout.json",
        "scripts/soho_pipe.py",
        "scripts/render_hip.py",
        "houdini/hda_source/Contents",
        "src/test.cpp",
    ):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture " + relative)
    for relative in ("dso/_cycles_houdini.dll", "bin/ch_worker.exe"):
        path = host / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"compiled fixture")
    return project, host, project / "houdini-sdk"


def generated(host):
    for relative in assets.OUTPUTS:
        path = host / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(name=relative)))


def snapshot(host):
    return {str(p.relative_to(host)): (p.read_bytes(), p.stat().st_mtime_ns) for p in host.rglob("*") if p.is_file()}


def rejected(action):
    try:
        action()
    except (RuntimeError, FileExistsError):
        return
    raise AssertionError("An invalid frozen asset operation was accepted")


project, host, sdk = fixture("solaris")
prepared = assets.check(project, host, sdk, True)
generated(host)
rejected(lambda: assets.seal(project, host, sdk, prepared["recipe_sha256"], True))
for relative in assets.SOLARIS_OUTPUTS:
    path = host / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
assets.seal(project, host, sdk, prepared["recipe_sha256"], True)
assert assets.check(project, host, sdk, True)["action"] == "reuse"
rejected(lambda: assets.check(project, host, sdk))
checks.append("Solaris requires its own complete inventory and cannot be reused as a native host")

project, host, sdk = fixture("normal")
prepared = assets.check(project, host, sdk)
assert prepared["action"] == "create"
generated(host)
assets.seal(project, host, sdk, prepared["recipe_sha256"])
before = snapshot(host)
assert assets.check(project, host, sdk)["action"] == "reuse"
for relative in assets.OUTPUTS:
    if relative.endswith(".json"):
        assert assets.frozen_json(host, relative, dict(name=relative))["action"] == "reuse"
assert snapshot(host) == before
checks.append("Unchanged reuse preserves every file byte and modification time")
rejected(lambda: assets.seal(project, host, sdk, prepared["recipe_sha256"]))
assert snapshot(host) == before
checks.append("The immutable inventory cannot be overwritten")

source = project / "scripts/create-light-asset.py"
source.write_text("changed generator")
rejected(lambda: assets.check(project, host, sdk))
assert snapshot(host) == before
checks.append("Changed generator is rejected before touching assets")

for dependency in (
    "create-blender-assets.py",
    "engine_source.py",
    "generate-node-help.py",
    "generate-property-help.py",
    "generate-viewer-help-icons.py",
    "pass-schema.py",
    "offline-scripts.py",
):
    project, host, sdk = fixture("changed-" + dependency)
    prepared = assets.check(project, host, sdk)
    generated(host)
    assets.seal(project, host, sdk, prepared["recipe_sha256"])
    before = snapshot(host)
    (project / "scripts" / dependency).write_text("changed recipe dependency")
    rejected(lambda: assets.check(project, host, sdk))
    assert snapshot(host) == before
    checks.append("Changed " + dependency + " invalidates the sealed recipe without touching assets")

project, host, sdk = fixture("legacy")
generated(host)
before = snapshot(host)
rejected(lambda: assets.check(project, host, sdk))
assert snapshot(host) == before
checks.append("A legacy host with unrecorded assets requires a fresh build")

project, host, sdk = fixture("tampered")
prepared = assets.check(project, host, sdk)
generated(host)
assets.seal(project, host, sdk, prepared["recipe_sha256"])
(host / assets.OUTPUTS[0]).write_text("damaged asset")
before = snapshot(host)
rejected(lambda: assets.check(project, host, sdk))
assert snapshot(host) == before
checks.append("A changed asset is rejected without repairing it in place")

for case in ("missing-python", "changed-kernel", "added-runtime"):
    project, host, sdk = fixture(case)
    prepared = assets.check(project, host, sdk)
    generated(host)
    python_file = host / "python3.13libs/light.py"
    python_file.parent.mkdir(parents=True)
    python_file.write_text("frozen Python")
    kernel = host / "resources/lib/kernel.zst"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"frozen kernel")
    assets.seal(project, host, sdk, prepared["recipe_sha256"])
    if case == "missing-python":
        python_file.unlink()
    elif case == "changed-kernel":
        kernel.write_bytes(b"changed kernel")
    else:
        (host / "unexpected.py").write_text("unexpected runtime")
    before = snapshot(host)
    rejected(lambda: assets.check(project, host, sdk))
    assert snapshot(host) == before
    checks.append("The complete frozen runtime rejects " + case + " without modifying it")

project, host, sdk = fixture("binary")
prepared = assets.check(project, host, sdk)
generated(host)
assets.seal(project, host, sdk, prepared["recipe_sha256"])
(host / "dso/_cycles_houdini.dll").write_bytes(b"changed compiled module")
before = snapshot(host)
rejected(lambda: assets.check(project, host, sdk))
assert snapshot(host) == before
checks.append("Changed private ABI requires a fresh host")

project, host, sdk = fixture("generation-race")
prepared = assets.check(project, host, sdk)
generated(host)
(project / "src/test.cpp").write_text("changed during asset authoring")
rejected(lambda: assets.seal(project, host, sdk, prepared["recipe_sha256"]))
assert not (host / assets.MARKER).exists()
checks.append("Inputs changed during generation cannot be sealed")

project, host, sdk = fixture("metadata")
assert assets.frozen_json(host, "metadata.json", dict(a=1, b=2), True)["action"] == "created"
before = snapshot(host)
assert assets.frozen_json(host, "metadata.json", dict(b=2, a=1))["action"] == "reuse"
rejected(lambda: assets.frozen_json(host, "metadata.json", dict(a=3)))
rejected(lambda: assets.frozen_json(host, "missing.json", dict(a=1)))
rejected(lambda: assets.frozen_json(host, "../../../../outside.json", dict(a=1), True))
assert snapshot(host) == before
checks.append("Frozen JSON compares structurally, refuses changes and contains paths")

build = project / "build/windows-h22-release"
assets.publish_json(build, "manifest.json", dict(generated_utc="first", value=1))
path = build / "manifest.json"
before = (path.read_bytes(), path.stat().st_mtime_ns)
assets.publish_json(build, "manifest.json", dict(generated_utc="later", value=1), True)
assert before == (path.read_bytes(), path.stat().st_mtime_ns)
assets.publish_json(build, "manifest.json", dict(generated_utc="later", value=2), True)
assert json.loads(path.read_text())["value"] == 2
checks.append("Identical public metadata is preserved; changes publish a complete JSON file")

report = dict(result="passed", checks=checks, fixture=str(destination))
(destination.parent / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(dict(result="passed", checks=len(checks))))
