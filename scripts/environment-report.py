# SPDX-License-Identifier: Apache-2.0
"""Record the actual host ABI, runtime dependencies, and benchmark hardware."""

import json
import importlib.util
import hashlib
import platform
from pathlib import Path
import re
import subprocess
import sys
import winreg

import hou
import OpenImageIO as OIIO
import PyOpenColorIO as OCIO
import PySide6
from PySide6 import QtCore
from pxr import Usd


output = Path(sys.argv[1])
source = Path(sys.argv[2])
build = Path(sys.argv[3])
include = Path(hou.getenv("HFS")) / "toolkit/include"

# Validate encoding and syntax without executing host callbacks or creating pyc
# files. A panel imported only by the GUI must not escape build-time validation.
for runtime_source in (source / "houdini").rglob("*.py"):
    compile(runtime_source.read_bytes(), str(runtime_source), "exec")


def macro(path, name):
    text = Path(path).read_text(encoding="utf-8")
    match = re.search(r"^\s*#\s*define\s+" + re.escape(name) + r"\s+([^\r\n]+)", text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"Missing SDK version macro {name} in {path}")
    return match[1].strip().strip('"')


compiler_files = list((build / "CMakeFiles").glob("*/CMakeCXXCompiler.cmake"))
if len(compiler_files) != 1:
    raise RuntimeError("Expected one compiler configuration in the certified build directory")
compiler_text = compiler_files[0].read_text(encoding="utf-8")
compiler = dict(
    path=re.search(r'set\(CMAKE_CXX_COMPILER "([^"]+)"\)', compiler_text)[1],
    version=re.search(r'set\(CMAKE_CXX_COMPILER_VERSION "([^"]+)"\)', compiler_text)[1],
    id=re.search(r'set\(CMAKE_CXX_COMPILER_ID "([^"]+)"\)', compiler_text)[1],
)
cache_text = (build / "CMakeCache.txt").read_text(encoding="utf-8")
roots = dict(
    cycles=source / "third_party/cycles",
    dependencies=source / "third_party/cycles/lib/windows_x64",
)
# A CPU-only build (scripts/build.ps1 -NoCuda) has no OptiX and needs no OptiX headers.
if not re.search(r"^WITH_CYCLES_DEVICE_OPTIX:BOOL=(?:OFF|FALSE|0)$", cache_text, re.M):
    roots["optix"] = source / "third_party/optix"
if re.search(r"^WITH_DLSS:BOOL=(?:ON|TRUE|1)$", cache_text, re.M):
    roots["dlss"] = source / "third_party/dlss"
revisions = {
    key: subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    for key, path in roots.items()
}
lock = json.loads((source / "source.lock.json").read_text(encoding="utf-8-sig"))
from engine_source import engine_identity, fingerprint

# The private route (fork copy) must have HEAD at cycles.authoring_tip; the
# public route (upstream plus patches/ via git am, new commit ids) is accepted
# by Git tree identity with cycles.authoring_tree. Uncommitted edits are caught
# by the clean-checkout check below.
engine = engine_identity(roots["cycles"], lock["cycles"])
expected = dict(
    cycles=engine["expected_head"],
    dependencies=lock["windows_dependencies"]["commit"],
)
if "optix" in roots:
    expected["optix"] = lock["optix_headers"]["commit"]
if "dlss" in roots:
    expected["dlss"] = lock["dlss_headers"]["commit"]
if revisions != expected:
    raise RuntimeError(f"Source revisions differ from source.lock.json: {revisions}")
# A commit and patch list alone do not identify unrecorded edits or new files.
# Keep a read-only fingerprint of the actual patched engine source as well.
source_fingerprint = dict(cycles=fingerprint(roots["cycles"]))
if source_fingerprint["cycles"]["dirty"]:
    raise RuntimeError("Production engine fork must be clean; export and lock source changes before building")
sdk = dict(
    houdini=hou.applicationVersionString(),
    hdk_api=int(macro(include / "UT/UT_HDKVersion.h", "HDK_API_VERSION")),
    usd=".".join(map(str, Usd.GetVersion())),
    openvdb=macro(include / "openvdb/version.h", "OPENVDB_LIBRARY_VERSION_STRING"),
    openimageio=OIIO.VERSION_STRING,
    opencolorio=OCIO.GetVersion(),
    pyside=PySide6.__version__,
    qt=QtCore.qVersion(),
    python=sys.version.split()[0],
)
sdk_spec = importlib.util.spec_from_file_location("ch_build_sdk_dependencies", source / "scripts/sdk-dependencies.py")
sdk_module = importlib.util.module_from_spec(sdk_spec)
sdk_spec.loader.exec_module(sdk_module)
dependency_providers = sdk_module.inspect(Path(hou.getenv("HFS")), source, build / "CMakeCache.txt")
sdk["openimagedenoise"] = dependency_providers["openimagedenoise"]["version"]
embree_header = source / "third_party/cycles/lib/windows_x64/embree/include/embree4/rtcore_config.h"
dependencies = dict(
    embree=macro(embree_header, "RTC_VERSION_STRING"),
    opencolorio_namespace=macro(include / "OpenColorIO/OpenColorABI.h", "OCIO_NAMESPACE"),
)
try:
    gpu_lines = (
        subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,uuid,pci.bus_id,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=15,
        )
        .strip()
        .splitlines()
    )
    gpus = [
        dict(zip(("name", "uuid", "pci_bus_id", "driver", "memory_mib"), [v.strip() for v in line.split(",")]))
        for line in gpu_lines
    ]
except (OSError, subprocess.SubprocessError) as error:
    gpus = dict(unavailable=str(error))
with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
    processor = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
working = OCIO.GetCurrentConfig().getColorSpace(OCIO.ROLE_SCENE_LINEAR)
report = dict(
    schema_version=1,
    sdk=sdk,
    compiler=compiler,
    actual_revisions=revisions,
    # How the engine source was obtained ("private" or "public"; "upstream" for a lock without a fork tip).
    engine_route=engine["route"],
    engine_tree=engine["tree"],
    dependencies=dependencies,
    engine_upstream_commit=lock["cycles"]["commit"],
    dependency_providers=dependency_providers,
    source_fingerprint=source_fingerprint,
    host=dict(os=platform.platform(), processor=processor, gpus=gpus),
    ocio=dict(config=hou.getenv("OCIO"), scene_linear=working.getName() if working else "Linear Rec.709"),
)
output.write_text(json.dumps(report, indent=2), encoding="utf-8")
