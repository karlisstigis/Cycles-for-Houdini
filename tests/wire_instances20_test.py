# SPDX-License-Identifier: Apache-2.0
"""Compile and execute the complete protocol92 codec with real TBB, without a renderer."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
from multi_device_range_fault_test import compiler_environment

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def block(text, pattern):
    found = [part for part in text.split("\n\n") if re.search(pattern, part)]
    assert len(found) == 1, (pattern, len(found))
    return dict(re.findall(r"^  ([A-Z_]+) = (.*)$", found[0], re.MULTILINE))


def file_version(path):
    version = ctypes.windll.version
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    data = ctypes.create_string_buffer(size)
    assert version.GetFileVersionInfoW(str(path), 0, size, data)
    pointer, length = ctypes.c_void_p(), ctypes.c_uint()
    assert version.VerQueryValueW(data, "\\", ctypes.byref(pointer), ctypes.byref(length))
    words = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))
    return ".".join(str(x) for x in (words[2] >> 16, words[2] & 65535, words[3] >> 16, words[3] & 65535))


def preflight(executable, search, environment):
    """Resolve ordinary imports without loading any candidate DLL into Python."""
    system = Path(environment["SYSTEMROOT"]) / "System32"
    directories = [executable.parent, system, Path(environment["SYSTEMROOT"]), *search]
    queue, seen, imports = [executable], set(), []
    while queue:
        binary = queue.pop()
        name = str(binary).casefold()
        if name in seen:
            continue
        seen.add(name)
        result = subprocess.run(
            ["dumpbin", "/nologo", "/dependents", str(binary)],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for dependency in re.findall(r"^\s+([^\s\\/:]+\.dll)\s*$", result.stdout, re.MULTILINE | re.IGNORECASE):
            if dependency.casefold().startswith(("api-ms-", "ext-ms-")):
                imports.append(dict(importer=str(binary), name=dependency, kind="Windows API set"))
                continue
            resolved = next(
                (directory / dependency for directory in directories if (directory / dependency).is_file()), None
            )
            assert resolved is not None, ("missing imported DLL", str(binary), dependency)
            resolved = resolved.resolve()
            imports.append(
                dict(
                    importer=str(binary),
                    name=dependency,
                    path=str(resolved),
                    sha256=sha(resolved),
                    version=file_version(resolved),
                )
            )
            if not resolved.is_relative_to(Path(environment["SYSTEMROOT"]).resolve()):
                queue.append(resolved)
    return imports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--output", "--destination", dest="output", type=Path, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--expected-poses", type=int)
    parser.add_argument("--expected-attributes", type=int)
    args = parser.parse_args()
    assert os.name == "nt", "This contract uses the certified MSVC dependency build"
    build, output = args.build.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = dict(result="running", classification="full production protocol92 codec; real TBB; no renderer/GPU")
    try:
        cache = (build / "CMakeCache.txt").read_text(encoding="utf-8")
        source = Path(re.search(r"^CMAKE_HOME_DIRECTORY:INTERNAL=(.*)$", cache, re.MULTILINE).group(1))
        engine = json.loads((ROOT / "source.lock.json").read_text())["cycles"]
        built = json.loads((source / "source.lock.json").read_text())["cycles"]
        assert (built["authoring_tip"], built.get("authoring_tree")) == (
            engine["authoring_tip"],
            engine.get("authoring_tree"),
        )
        # Private route: HEAD is the locked tip. Public route: same Git tree, new commit ids.
        identity = runpy.run_path(str(ROOT / "scripts/engine_source.py"))["engine_identity"]
        assert identity(source / "third_party/cycles", engine)["route"] in ("private", "public")
        headers = {}
        for header in sorted((ROOT / "include").rglob("*.h")):
            relative = header.relative_to(ROOT)
            digest = sha(header)
            assert sha(source / relative) == digest, ("dependency public header differs", str(relative))
            headers[str(relative)] = digest
        assert re.search(r"^CH_IMAGE_IRIS:BOOL=(?:ON|TRUE|1)$", cache, re.MULTILINE), "Image Iris must remain enabled"
        dependencies = dict(
            source=str(source),
            engine=engine,
            public_headers=headers,
            core_library_sha256=sha(build / "lib/ch_core.lib"),
        )
        report["dependencies"] = dependencies
        ninja = (build / "build.ninja").read_text(encoding="utf-8")
        compile_vars = block(ninja, r"^build .*snapshot_wire\.cpp\.obj:")
        link_vars = block(ninja, r"^build bin\\ch_harness\.exe:")
        production, harness = ROOT / "src/core/snapshot_wire.cpp", ROOT / "tests/wire_instances20_fixture.cpp"
        cpp = output / "wire_instances20_full.cpp"
        cpp.write_text('#include "' + production.as_posix() + '"\n' + harness.read_text(), encoding="utf-8")
        obj, exe = output / "wire_instances20.obj", output / "wire_instances20.exe"
        # Windows variable names are case-insensitive. Avoid duplicate Path/PATH
        # entries from the developer prompt overriding the declared DLL path.
        environment = {key.upper(): value for key, value in compiler_environment(None).items()}
        # Reproduce the selected build's declared runtime, including its private host DLLs.
        host_paths, package_paths = [], []
        for package in sorted((build / "packages").glob("*.json")):
            for entry in json.loads(package.read_text())["env"]:
                if isinstance(entry.get("CYCLES_HOUDINI_HOST_ROOT"), str):
                    host_paths.append(str(Path(entry["CYCLES_HOUDINI_HOST_ROOT"]) / "bin"))
                if isinstance(entry.get("PATH"), dict):
                    package_paths.extend(entry["PATH"]["value"])
        assert host_paths and package_paths, "selected dependency build has no runtime package"
        declared = [*host_paths, str(build / "bin"), *package_paths]
        assert not any("$" in path for path in declared), "unresolved package path"
        environment["PATH"] = os.pathsep.join([*declared, environment["PATH"]])
        dependencies["dll_search_paths"] = declared
        report.update(
            source_sha256=sha(production),
            harness_sha256=sha(harness),
            identity=json.loads(os.environ.get("CYCLES_HOUDINI_TEST_IDENTITY", "{}")),
        )
        compile_command = (
            "cl /nologo /TP "
            + " ".join(compile_vars[x] for x in ("DEFINES", "INCLUDES", "FLAGS"))
            + f' /Fo"{obj}" /Fd"{output / "compile.pdb"}" /c "{cpp}"'
        )
        link_command = (
            f'link /nologo "{obj}" '
            + link_vars["LINK_LIBRARIES"]
            + f' /out:"{exe}" /pdb:"{output / "wire_instances20.pdb"}" /INCREMENTAL:NO /SUBSYSTEM:CONSOLE'
        )
        for name, command in (("compile", compile_command), ("link", link_command)):
            result = subprocess.run(
                command,
                cwd=build,
                env=environment,
                text=True,
                capture_output=True,
                timeout=240,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            (output / (name + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
            assert result.returncode == 0, (name, result.returncode)
        dependencies["resolved_imports"] = preflight(
            exe, [Path(x) for x in environment["PATH"].split(os.pathsep) if x], environment
        )
        report["executable_sha256"] = sha(exe)
        replay_args = [str(args.fixture.resolve())] if args.fixture else []
        if args.expected_poses is not None or args.expected_attributes is not None:
            assert args.fixture and args.expected_poses is not None and args.expected_attributes is not None
            replay_args.extend([str(args.expected_poses), str(args.expected_attributes)])
        result = subprocess.run(
            [str(exe)] + replay_args,
            cwd=output,
            env=environment,
            text=True,
            capture_output=True,
            timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        (output / "run.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        report["executable_exit"] = result.returncode
        assert result.returncode == 0, (result.returncode, result.stderr)
        report.update(result="passed", contracts=json.loads(result.stdout.splitlines()[-1]))
    except BaseException as error:
        report.update(result="failed", error=repr(error))
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
