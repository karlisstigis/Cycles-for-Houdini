# SPDX-License-Identifier: Apache-2.0
"""Pinned SDK dependency identities; safe to import without loading Cycles."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import threading
import time
import sys

_cache = {}
_lock = threading.Lock()
_compatibility_warning_sent = False


class SDKDependencyError(RuntimeError):
    """An explicitly diagnosed unsupported or missing SDK dependency."""


class SDKModuleEnumerationUnavailable(SDKDependencyError):
    """The process loader's module list is not presently available."""


def check_houdini_edition():
    try:
        import hou
    except ModuleNotFoundError as error:
        if error.name != "hou":
            raise
        return
    if hou.licenseCategory() in (hou.licenseCategoryType.Apprentice, hou.licenseCategoryType.ApprenticeHD):
        raise SDKDependencyError("Cycles for Houdini requires Houdini Indie, Core or FX. Apprentice is unsupported.")


def require(value, message):
    if not value:
        raise SDKDependencyError("Cycles Houdini SDK dependency: " + message)


def macro(path, name):
    text = Path(path).read_text(encoding="utf-8")
    match = re.search(r"^\s*#\s*define\s+" + re.escape(name) + r"\s+([^\r\n]+)", text, re.MULTILINE)
    require(match, f"missing {name} in {path}")
    return match[1].strip().strip('"')


def major_minor(version):
    match = re.fullmatch(r"(\d+)\.(\d+)\.\d+", version)
    require(match, f"invalid Houdini build version: {version}")
    return match.group(1, 2)


def check_host_compatibility(built_version, built_api, running_version, running_api, strict=False):
    require(
        major_minor(built_version) == major_minor(running_version),
        f"unsupported Houdini {running_version}; built for {built_version}",
    )
    require(
        int(built_api) == int(running_api),
        f"incompatible HDK API {running_api} in Houdini {running_version}; built for API {built_api} in {built_version}",
    )
    require(
        not strict or built_version == running_version,
        f"strict build matching rejects Houdini {running_version}; built for {built_version}",
    )
    return built_version != running_version


def warn_compatible_build(built_version, running_version):
    global _compatibility_warning_sent
    with _lock:
        if _compatibility_warning_sent:
            return
        _compatibility_warning_sent = True
    message = f"Cycles for Houdini: built for {built_version}; running compatible Houdini {running_version} (not separately qualified)"
    print(message, file=sys.stderr)
    try:
        import hou

        if hou.isUIAvailable():
            hou.ui.setStatusMessage(message, severity=hou.severityType.Warning)
    except (ImportError, AttributeError, RuntimeError):
        pass


def relative(root, name):
    root = Path(root).resolve()
    require(
        isinstance(name, str) and name and not Path(name).is_absolute() and ".." not in Path(name).parts,
        "invalid SDK-relative path",
    )
    path = (root / name).resolve()
    require(path.is_relative_to(root), "SDK path leaves its installation")
    return path


def file_version(path):
    require(os.name == "nt", "this native milestone requires Windows")
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    version.VerQueryValueW.argtypes = (
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    )
    unused = wintypes.DWORD()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
    require(size, f"missing Windows file version in {path}")
    buffer = ctypes.create_string_buffer(size)
    require(version.GetFileVersionInfoW(str(path), 0, size, buffer), f"cannot read file version: {path}")
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    require(
        version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) and length.value >= 52,
        f"invalid file version: {path}",
    )
    fixed = struct.unpack_from("<13I", ctypes.string_at(pointer, length.value))
    require(fixed[0] == 0xFEEF04BD, f"invalid Windows version resource: {path}")
    return ".".join(str(value) for value in (fixed[2] >> 16, fixed[2] & 65535, fixed[3] >> 16, fixed[3] & 65535))


def identity(path, runtime=False):
    path = Path(path).resolve()
    stat = path.stat()
    key = (str(path).casefold(), stat.st_size, stat.st_mtime_ns, runtime)
    with _lock:
        found = _cache.get(key)
    if found is not None:
        return dict(found)
    with path.open("rb") as stream:
        value = dict(bytes=stat.st_size, sha256=hashlib.file_digest(stream, "sha256").hexdigest())
    if runtime:
        value["version"] = file_version(path)
    with _lock:
        _cache[key] = value
    return dict(value)


def inspect_sdk(hfs, policy):
    hfs = Path(hfs).resolve()
    require(policy.get("provider") == "houdini-sdk", "only the pinned Houdini SDK OIDN provider is supported")
    actual_host = macro(hfs / "toolkit/include/SYS/SYS_Version.h", "SYS_VERSION_FULL")
    require(
        major_minor(actual_host) == major_minor(policy["houdini"]),
        f"unsupported Houdini {actual_host}; expected {policy['houdini']} major/minor",
    )
    hdk_api = int(macro(hfs / "toolkit/include/UT/UT_HDKVersion.h", "HDK_API_VERSION"))
    actual = macro(hfs / "toolkit/include/OpenImageDenoise/config.h", "OIDN_VERSION_STRING")
    require(actual == policy["version"], f"unsupported OIDN headers {actual}; expected {policy['version']}")
    names = [("header", "toolkit/include/OpenImageDenoise/" + name) for name in ("config.h", "oidn.h", "oidn.hpp")]
    names += [("import-library", "custom/houdini/dsolib/OpenImageDenoise.lib")]
    names += [("runtime", "bin/" + name) for name in policy["runtime_files"]]
    files = []
    for kind, name in names:
        path = relative(hfs, name)
        require(path.is_file(), "missing SDK dependency: " + str(path))
        record = dict(path=name, kind=kind, **identity(path, kind == "runtime"))
        if kind == "runtime":
            require(
                record["version"] == policy["version"] + ".0",
                f"wrong OIDN runtime version in {path}: {record['version']}",
            )
        files.append(record)
    return dict(
        provider="houdini-sdk", houdini=actual_host, hdk_api=hdk_api, version=actual, redistributed=False, files=files
    )


def validate_cache(cache, hfs):
    values = {}
    for line in Path(cache).read_text(encoding="utf-8").splitlines():
        match = re.match(r"([^/#][^:]*):[^=]+=(.*)", line)
        if match:
            values[match[1]] = match[2]
    hfs = Path(hfs).resolve()
    expected = {
        "OPENIMAGEDENOISE_INCLUDE_DIR": hfs / "toolkit/include",
        "OPENIMAGEDENOISE_LIBRARY": hfs / "custom/houdini/dsolib/OpenImageDenoise.lib",
        "OPENIMAGEDENOISE_OPENIMAGEDENOISE_LIBRARY": hfs / "custom/houdini/dsolib/OpenImageDenoise.lib",
    }
    require(values.get("CH_OIDN_PROVIDER") == "houdini-sdk", "CMake did not select the SDK OIDN provider; reconfigure")
    for name, path in expected.items():
        require(Path(values.get(name, "")).resolve() == path, f"{name} does not select the pinned SDK path")
    for component in ("COMMON", "DNNL_CPU", "DNNL_COMMON", "MKLDNN", "DNNL"):
        value = values.get("OPENIMAGEDENOISE_" + component + "_LIBRARY", "")
        require(
            not value or value.endswith("-NOTFOUND"), "OIDN must not link competing static " + component + " components"
        )
    return values


def loaded_modules(pid=None):
    require(os.name == "nt", "module verification requires Windows")
    kernel, psapi = ctypes.WinDLL("kernel32", use_last_error=True), ctypes.WinDLL("psapi", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    psapi.EnumProcessModulesEx.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HMODULE),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    )
    psapi.GetModuleFileNameExW.argtypes = (wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD)
    psapi.GetModuleFileNameExW.restype = wintypes.DWORD
    handle = kernel.OpenProcess(0x400 | 0x10, False, pid or os.getpid())
    if not handle:
        raise SDKModuleEnumerationUnavailable(
            "Cycles Houdini SDK dependency: cannot observe renderer dependency provider "
            "(Windows error " + str(ctypes.get_last_error()) + ")"
        )
    result = []
    try:
        capacity = 1024
        while True:
            buffer = (wintypes.HMODULE * capacity)()
            needed = wintypes.DWORD()
            if not psapi.EnumProcessModulesEx(handle, buffer, ctypes.sizeof(buffer), ctypes.byref(needed), 3):
                raise SDKModuleEnumerationUnavailable(
                    "Cycles Houdini SDK dependency: cannot enumerate renderer dependencies "
                    "(Windows error " + str(ctypes.get_last_error()) + ")"
                )
            if needed.value <= ctypes.sizeof(buffer):
                break
            capacity = needed.value // ctypes.sizeof(wintypes.HMODULE) + 128
        for index in range(needed.value // ctypes.sizeof(wintypes.HMODULE)):
            name = ctypes.create_unicode_buffer(32768)
            length = psapi.GetModuleFileNameExW(handle, buffer[index], name, len(name))
            if not length or length >= len(name):
                raise SDKModuleEnumerationUnavailable(
                    "Cycles Houdini SDK dependency: cannot identify every renderer module "
                    "(Windows error " + str(ctypes.get_last_error()) + ")"
                )
            path = Path(name.value).resolve()
            if path.name.lower().startswith("openimagedenoise"):
                result.append(path)
    finally:
        kernel.CloseHandle(handle)
    return result


def observe_host_modules(timeout=0.5):
    """A changing loader list cannot be treated as a complete attestation."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return loaded_modules()
        except SDKModuleEnumerationUnavailable:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def validate_modules(paths, hfs, record, require_loaded=True):
    expected = {relative(hfs, entry["path"]): entry for entry in record["files"] if entry["kind"] == "runtime"}
    loaded = []
    for path in paths:
        path = Path(path).resolve()
        require(
            path in expected,
            "unexpected/preloaded OIDN provider: " + str(path) + "; restart with the pinned Houdini SDK",
        )
        actual = identity(path, True)
        reference = expected[path]
        require(
            all(actual[key] == reference[key] for key in ("bytes", "sha256", "version")),
            "loaded OIDN file differs from the built SDK: " + str(path),
        )
        loaded.append(dict(path=str(path), **actual))
    if require_loaded:
        require(
            any(Path(entry["path"]).name.lower() == "openimagedenoise.dll" for entry in loaded),
            "OIDN main library was not loaded",
        )
    return loaded


def runtime_policy():
    host = Path(os.environ["CYCLES_HOUDINI_HOST_ROOT"])
    path = host / "resources/sdk-dependencies.json"
    require(path.is_file(), "the staged SDK dependency policy is missing; build a fresh host")
    document = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(document, dict), "malformed staged SDK dependency document")
    record = document["openimagedenoise"]
    require(isinstance(record, dict), "invalid staged OIDN provider metadata")
    require(
        record.get("provider") == "houdini-sdk"
        and record.get("version") == "2.3.3"
        and isinstance(record.get("houdini"), str)
        and isinstance(record.get("hdk_api"), int),
        "unsupported staged OIDN provider policy",
    )
    require(isinstance(record.get("files"), list) and record["files"], "missing staged OIDN file identities")
    for entry in record["files"]:
        require(
            isinstance(entry, dict) and all(key in entry for key in ("path", "kind", "bytes", "sha256")),
            "malformed staged SDK file identity",
        )
        require(
            entry["kind"] in ("header", "import-library", "runtime")
            and isinstance(entry["bytes"], int)
            and isinstance(entry["sha256"], str)
            and bool(re.fullmatch("[0-9a-f]{64}", entry["sha256"])),
            "invalid staged SDK file identity",
        )
        if entry["kind"] == "runtime":
            require(entry.get("version") == record["version"] + ".0", "invalid staged SDK runtime version")
        relative(Path("."), entry["path"])
    expected = {"toolkit/include/OpenImageDenoise/" + name for name in ("config.h", "oidn.h", "oidn.hpp")}
    expected.add("custom/houdini/dsolib/OpenImageDenoise.lib")
    expected.update("bin/OpenImageDenoise" + suffix + ".dll" for suffix in ("", "_core", "_device_cpu", "_device_cuda"))
    require(
        len(record["files"]) == len(expected) and {entry["path"] for entry in record["files"]} == expected,
        "incomplete or unexpected staged SDK dependency file inventory",
    )
    return record


def validate_runtime(pid=None, require_loaded=False):
    try:
        return _validate_runtime(pid, require_loaded)
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError) as error:
        raise SDKDependencyError(
            "Cycles Houdini SDK dependency: missing/unreadable provider metadata or SDK file: " + str(error)
        ) from error


def _validate_runtime(pid, require_loaded):
    record = runtime_policy()
    hfs = Path(os.environ["HFS"]).resolve()
    running_version = macro(hfs / "toolkit/include/SYS/SYS_Version.h", "SYS_VERSION_FULL")
    running_api = macro(hfs / "toolkit/include/UT/UT_HDKVersion.h", "HDK_API_VERSION")
    compatible_other_build = check_host_compatibility(
        record["houdini"],
        record["hdk_api"],
        running_version,
        running_api,
        os.environ.get("CYCLES_HOUDINI_STRICT_BUILD") == "1",
    )
    for entry in record["files"]:
        if entry["kind"] != "runtime":
            continue
        path = relative(hfs, entry["path"])
        require(path.is_file(), "required SDK runtime is missing: " + str(path))
        actual = identity(path, True)
        require(
            all(actual[key] == entry[key] for key in ("bytes", "sha256", "version")),
            "installed SDK OIDN differs from the compiled policy: " + str(path),
        )
    paths = observe_host_modules() if pid is None else loaded_modules(pid)
    loaded = validate_modules(paths, hfs, record, require_loaded)
    if compatible_other_build:
        warn_compatible_build(record["houdini"], running_version)
    return dict(provider=record["provider"], version=record["version"], houdini=record["houdini"], loaded=loaded)


def wait_for_worker_provider(process, timeout=30.0):
    """Verify imported DLLs before sending configuration or invoking the engine.

    A first launch on a cold disk, or under real-time malware scanning of the
    large SDK OIDN DLLs, can take several seconds. An exited worker still
    fails immediately; only a slow, live start uses the longer bound.
    """
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        try:
            value = validate_runtime(process.pid)
            if any(Path(entry["path"]).name.lower() == "openimagedenoise.dll" for entry in value["loaded"]):
                return value
        except SDKModuleEnumerationUnavailable:
            # The loader may not have initialized its module list when Popen
            # returns. Only this observation failure gets the bounded grace.
            pass
        require(time.monotonic() < deadline, "worker did not load the SDK OIDN provider")
        time.sleep(0.01)
    raise SDKDependencyError(
        "Cycles Houdini SDK dependency: worker exited before provider verification (exit "
        + str(process.returncode)
        + ")"
    )


def worker_environment():
    environment = dict(os.environ)
    sdk_bin = str(Path(environment["HFS"]).resolve() / "bin")
    paths = [
        path
        for path in environment.get("PATH", "").split(os.pathsep)
        if path and os.path.normcase(path) != os.path.normcase(sdk_bin)
    ]
    environment["PATH"] = os.pathsep.join([sdk_bin, *paths])
    return environment
