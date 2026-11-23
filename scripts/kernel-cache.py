# SPDX-License-Identifier: Apache-2.0
"""Compile the GPU kernels once, reuse them for further builds (community builds only).

The engine compiles its GPU kernels (CUDA kernel_sm_*.cubin / kernel_compute_*.ptx and the OptiX
kernel_optix*.ptx) with NVIDIA's compiler nvcc; that takes about 7 of the 11 minutes of a build, and
the result does not depend on the Houdini build. scripts/build.ps1 -KernelCache <folder> points the
engine's kernel rules at a small stand-in for nvcc (the engine's own CUDA<major>_NVCC_EXECUTABLE
setting, src/kernel/device/cuda/CMakeLists.txt cuda_get_toolkit_for_arch; no engine, patch or CMake
change). For every kernel compile the stand-in works out a key from everything the kernel is made
from and either copies the kernel file from the cache folder or runs nvcc unchanged and stores its
result. Every other nvcc call (--version, --list-gpu-code, ...) goes to nvcc unchanged.

The key (KEY_SCHEMA) covers:
  - the nvcc command line, with the build folder written as <BUILD> (the only part that differs
    between the Houdini builds of one source folder) and the working folder;
  - the steps nvcc would run (nvcc --dryrun of the same command, temporary file names removed):
    which programs, with which options and settings;
  - the source as the GPU compiler sees it: nvcc's own device preprocessing step (from --dryrun, run
    on its own into a temporary file), so every header, every macro and every include search
    result counts, including files that are new or now found first;
  - every file named in that preprocessed source (the kernel source and every header it includes,
    from the engine, the CUDA headers, the OptiX headers and the C++ compiler's headers) by content,
    so that even a change that leaves the preprocessed source the same (a comment) is a new key;
  - every file of the CUDA compiler folders bin, nvvm and include, by content (nvcc, cicc, ptxas,
    libdevice, nvcc.profile, ...), and every file next to any other program the steps run (cl.exe);
  - the settings nvcc and its steps read (NVCC_*, CUDA_*, INCLUDE, ...); PATH only through the programs
    it finds (above), because its order can differ between two runs with the same inputs;
  - this file itself.
Anything that cannot be worked out (an unexpected --dryrun, a missing file, any error) means:
compile with nvcc as without this tool, store nothing.

Existing build outputs are rechecked before Ninja runs, including changes to tool or input bytes
that a Ninja rule would otherwise miss. Missing validation causes the affected kernel to rebuild.

A cache entry is <cache>/<key>/ with the kernel file and manifest.json (its key, size and SHA-256),
written last into a temporary folder that is then renamed: an entry without a manifest, with
another key or with a file whose size or SHA-256 differs is ignored and removed, never used. After
a successful build `prune` keeps the newest two entries per kernel file name. Delete the cache
folder to clear it.

  kernel-cache.py prepare --build B --cache C --nvcc N --cuda-root R   # wrapper + settings into B/kernel-reuse
  kernel-cache.py nvcc <B/kernel-reuse/config.json> <real nvcc> <nvcc arguments>     # the stand-in (B/kernel-reuse/nvcc.cmd)
  kernel-cache.py report --build B [--ninja-log-offset N] [--mode on|off]   # one plain line, last-build.json
  kernel-cache.py prune --cache C [--keep 2]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

KEY_SCHEMA = 2
REUSE_FOLDER = "kernel-reuse"
KERNEL_SUFFIXES = (".cubin", ".ptx")
COMPILER_FOLDERS = ("bin", "nvvm", "include")
ENV_PREFIXES = ("NVCC", "CUDA", "NVVM", "PTXAS", "CICC", "FATBIN", "NVLINK")
# PATH itself is not part of the key: its order and repeated entries differ between two runs with the same inputs
# (measured: the first run and a later menu run of one project had the same 114 entries in another order). What it
# decides, which programs run, is in the key: the CUDA folders by content, every other program's folder by its
# files (other_programs, found the way the step finds it).
ENV_NAMES = ("INCLUDE", "EXTERNAL_INCLUDE", "CL", "_CL_", "TMP", "TEMP", "TMPDIR")
REASONS = {
    "reused": "nothing it is made from changed",
    "new": "first compile with these sources",
    "damaged": "a stored copy was damaged and was ignored",
    "unusable": "the reuse check could not be done, so it was compiled as usual",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def folder_fingerprint(root: Path, recursive: bool = True) -> list:
    """Sorted [relative path, size, SHA-256] of every file (recursive) or of the files directly in root."""
    root = Path(root)
    files = root.rglob("*") if recursive else root.iterdir()
    rows = []
    for path in files:
        if path.is_file():
            rows.append([path.relative_to(root).as_posix(), path.stat().st_size, sha256_file(path)])
    rows.sort()
    return rows


def compiler_fingerprint(cuda_root: Path) -> dict:
    cuda_root = Path(cuda_root)
    found = {}
    for name in COMPILER_FOLDERS:
        folder = cuda_root / name
        found[name] = folder_fingerprint(folder) if folder.is_dir() else None
    if not found["bin"]:
        raise RuntimeError(f"No CUDA compiler in {cuda_root / 'bin'}")
    return {"digest": hashlib.sha256(canonical(found)).hexdigest(), "files": sum(len(v or []) for v in found.values())}


class Normalizer:
    """Writes the build folder as <BUILD> (both slash directions, any letter case) and temporary names neutrally."""

    def __init__(self, build: str, temp: str | None = None):
        self.rules = []
        for value, token in ((build, "<BUILD>"), (temp, "<TEMP>")):
            if not value:
                continue
            value = os.path.normpath(value).rstrip("\\/")
            variants = {value, value.replace("\\", "/"), value.replace("/", "\\"), value.replace("\\", "\\\\")}
            for variant in sorted(variants, key=len, reverse=True):
                self.rules.append((re.compile(re.escape(variant), re.IGNORECASE), token))

    def text(self, value: str) -> str:
        for pattern, token in self.rules:
            value = pattern.sub(token, value)
        return re.sub(r"tmpxft_[0-9a-fA-F]+_[0-9a-fA-F]+", "tmpxft", value)


def kernel_compile(args: list[str]):
    """(output path, input .cu) when these nvcc arguments compile one kernel file, else None."""
    if sum(1 for a in args if a in ("--cubin", "--ptx", "-cubin", "-ptx")) != 1:
        return None
    if args.count("-o") != 1 or args.index("-o") + 1 >= len(args):
        return None
    if any(a in ("--dryrun", "-M", "-MD", "-E", "--run", "-run", "--keep", "-keep") for a in args):
        return None
    output = args[args.index("-o") + 1]
    if not output.lower().endswith(KERNEL_SUFFIXES):
        return None
    inputs = [a for a in args if a.lower().endswith(".cu") and not a.startswith("-")]
    if len(inputs) != 1:
        return None
    return output, inputs[0]


DRYRUN_SET = re.compile(r"^#\$ ([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_dryrun(text: str):
    """nvcc --dryrun prints '#$ NAME=value' settings and '#$ command' steps."""
    settings, steps = [], []
    for line in text.splitlines():
        if not line.startswith("#$ "):
            continue
        match = DRYRUN_SET.match(line)
        if match and " " not in match.group(1):
            settings.append((len(steps), match.group(1), match.group(2)))
        else:
            steps.append(line[3:])
    return settings, steps


# The output redirection of a step; nvcc puts it anywhere in the line (cl.exe ... > "x.cpp1.ii" -D... -E ...).
REDIRECT = re.compile(r'(?<=\s)>\s*("[^"]+"|[^\s"]+)')


def preprocess_step(steps: list[str], source: str):
    """The one device preprocessing step: -E on the kernel source, written with '>' into a file."""
    name = os.path.basename(source).lower()
    found = [
        (i, s)
        for i, s in enumerate(steps)
        if " -E " in f" {s} " and name in s.lower() and len(REDIRECT.findall(s)) == 1
    ]
    if len(found) != 1:
        raise RuntimeError(f"expected one device preprocessing step in nvcc --dryrun, found {len(found)}")
    return found[0]


def run_shell(command: str, env: dict, cwd: str):
    if os.name == "nt":
        return subprocess.run(f'cmd /d /s /c "{command}"', env=env, cwd=cwd, capture_output=True)
    return subprocess.run(["/bin/sh", "-c", command], env=env, cwd=cwd, capture_output=True)


LINE_MARKER = re.compile(rb'^\s*#\s*(?:line\s+)?\d+\s+"((?:[^"\\]|\\.)*)"', re.MULTILINE)


def named_files(preprocessed: bytes, cwd: str) -> list[str]:
    names = set()
    for match in LINE_MARKER.finditer(preprocessed):
        raw = match.group(1).decode("utf-8", "surrogateescape").replace("\\\\", "\\")
        if raw.startswith("<") or not raw:
            continue
        names.add(os.path.normcase(os.path.normpath(os.path.join(cwd, raw))))
    return sorted(names)


def step_settings(settings, env: dict, index: int) -> dict:
    """The environment of step `index`: the settings nvcc prints before it, over the caller's environment."""
    values = dict(env)
    for position, name, value in settings:
        if position <= index:
            values[name] = value
    return values


def program_paths(steps: list[str], settings, env: dict) -> list[str]:
    """Every program the steps run, as a full path: %VARIABLES% expanded, bare names looked up in the step's PATH.
    A program that cannot be found makes the key unusable (compile as usual)."""
    programs = set()
    for index, step in enumerate(steps):
        values = step_settings(settings, env, index)
        line = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", lambda m: values.get(m.group(1), m.group(0)), step).lstrip()
        if line.startswith(
            '""'
        ):  # nvcc writes ""%CICC_PATH%\cicc" ... for cmd.exe: the program is the first quoted part
            line = line[1:]
        match = re.match(r'^\s*"([^"]+)"|^\s*(\S+)', line)
        program = (match.group(1) or match.group(2)) if match else ""
        if not program:
            continue
        if not os.path.isabs(program):
            path = next((v for k, v in values.items() if k.upper() == "PATH"), None)
            found = shutil.which(program, path=path)
            if not found:
                raise RuntimeError(f"program of an nvcc step not found: {program}")
            program = found
        programs.add(os.path.normcase(os.path.normpath(program)))
    return sorted(programs)


def compute_key(config: dict, args: list[str], cwd: str, env: dict, scratch: Path) -> dict:
    output, source = kernel_compile(args)
    norm = Normalizer(config["build"], tempfile.gettempdir())
    nvcc = config["nvcc"]
    dry = subprocess.run([nvcc, "--dryrun", *args], env=env, cwd=cwd, capture_output=True, text=True, errors="replace")
    if dry.returncode != 0:
        raise RuntimeError(f"nvcc --dryrun failed ({dry.returncode})")
    settings, steps = parse_dryrun(dry.stdout + "\n" + dry.stderr)
    index, step = preprocess_step(steps, source)
    step_env = step_settings(settings, env, index)
    target = scratch / "device.ii"
    command = REDIRECT.sub(lambda _: f' > "{target}"', step)
    started = time.time()
    result = run_shell(command, step_env, cwd)
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError(f"device preprocessing failed ({result.returncode})")
    preprocessed = target.read_bytes()
    files = named_files(preprocessed, cwd)
    source_path = os.path.normcase(os.path.normpath(os.path.join(cwd, source)))
    if source_path not in files:
        files.append(source_path)
    inputs = []
    for name in sorted(files):
        path = Path(name)
        if not path.is_file():
            raise RuntimeError(f"a file named in the preprocessed source is missing: {name}")
        inputs.append([norm.text(name), sha256_file(path)])
    cuda_root = os.path.normcase(os.path.normpath(config["cuda_root"]))
    others = {}
    for program in program_paths(steps, settings, env):
        if not program.startswith(cuda_root + os.sep):
            folder = Path(program).parent
            others[norm.text(str(folder))] = hashlib.sha256(
                canonical(folder_fingerprint(folder, recursive=False))
            ).hexdigest()
    names = sorted(k for k in env if k.upper() in ENV_NAMES or k.upper().startswith(ENV_PREFIXES))
    parts = {
        "schema": KEY_SCHEMA,
        "tool": sha256_file(Path(__file__)),
        "nvcc": norm.text(os.path.normcase(os.path.normpath(nvcc))),
        "arguments": [norm.text(a) for a in args],
        "working_folder": norm.text(os.path.normcase(os.path.normpath(cwd))),
        "settings": {k.upper(): norm.text(env[k]) for k in names},
        "dryrun": [norm.text(s) for s in steps]
        + [f"{p}:{n}={'<programs below>' if n.upper() == 'PATH' else norm.text(v)}" for p, n, v in settings],
        "compiler": config["compiler"],
        "other_programs": others,
        "preprocessed": hashlib.sha256(
            norm.text(preprocessed.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
        ).hexdigest(),
        "inputs": inputs,
    }
    key = hashlib.sha256(canonical(parts)).hexdigest()
    return {"key": key, "parts": parts, "output": output, "seconds": round(time.time() - started, 2)}


def entry_folder(cache: Path, key: str) -> Path:
    return Path(cache) / key[:40]


def read_entry(cache: Path, key: str, name: str):
    """The cached kernel file for this key, or (None, why)."""
    folder = entry_folder(cache, key)
    if not folder.exists():
        return None, "new"
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        stored = folder / manifest["output"]
        ok = (
            manifest.get("schema") == KEY_SCHEMA
            and manifest.get("key") == key
            and manifest.get("output") == name
            and stored.is_file()
            and stored.stat().st_size == manifest["bytes"]
            and sha256_file(stored) == manifest["sha256"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        shutil.rmtree(folder, ignore_errors=True)
        return None, "damaged"
    try:
        (folder / "last-used").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), encoding="ascii")
    except OSError:
        pass  # only used to choose which old entries to keep
    return (stored, manifest["sha256"]), "reused"


def store_entry(cache: Path, key: str, parts: dict, produced: Path, seconds: float):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    final = entry_folder(cache, key)
    temporary = cache / f".partial-{key[:40]}-{uuid.uuid4().hex[:8]}"
    temporary.mkdir()
    try:
        shutil.copyfile(produced, temporary / produced.name)
        manifest = {
            "schema": KEY_SCHEMA,
            "key": key,
            "output": produced.name,
            "bytes": (temporary / produced.name).stat().st_size,
            "sha256": sha256_file(temporary / produced.name),
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "compile_seconds": round(seconds, 1),
            "made_from": parts,
        }
        if manifest["sha256"] != sha256_file(produced):
            raise RuntimeError("copy differs")
        with open(temporary / "manifest.json", "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        # Other builds may publish the same key. An existing valid copy wins.
        if final.exists() and read_entry(cache, key, produced.name)[0] is not None:
            return
        temporary.rename(final)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def place(stored: Path, output: Path, expected_sha256: str):
    partial = output.with_name(output.name + ".kernel-reuse-partial")
    try:
        shutil.copyfile(stored, partial)
        if sha256_file(partial) != expected_sha256:
            raise RuntimeError("copy differs from the verified manifest")
        os.replace(partial, output)
    finally:
        partial.unlink(missing_ok=True)


DECISIONS = "decisions"


def record(config: dict, entry: dict):
    """One file per decision: the kernel rules run in parallel, and appends to one shared file from several
    processes can overwrite each other on Windows (one test run lost the line of a kernel reused in the same instant
    as another). Written under a temporary name and renamed, so a file is either complete or absent."""
    folder = Path(config["build"]) / REUSE_FOLDER / DECISIONS
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    partial = folder / (name + ".partial")
    partial.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(partial, folder / (name + ".json"))


def read_decisions(build: Path) -> list:
    folder = Path(build) / REUSE_FOLDER / DECISIONS
    if not folder.is_dir():
        return []
    decisions = []
    for path in sorted(folder.glob("*.json")):
        try:
            decisions.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return decisions


def run_nvcc(nvcc: str, args: list[str]) -> int:
    return subprocess.run([nvcc, *args]).returncode


def scratch_folder(config):
    return tempfile.TemporaryDirectory(prefix="check-", dir=Path(config["build"]) / REUSE_FOLDER)


def verified_outputs(build):
    # Only the CUDA and OptiX rule outputs in this build tree may be invalidated.
    folders = build / "cycles/src/kernel/device"
    for device in ("cuda", "optix"):
        folder = folders / device
        if folder.is_dir() and folder.resolve().is_relative_to(build.resolve()):
            yield from (p for p in folder.glob("kernel_*") if p.suffix in KERNEL_SUFFIXES and p.is_file())


def validation_file(build, output):
    relative = output.resolve().relative_to(build.resolve()).as_posix()
    return build / REUSE_FOLDER / "verified" / (hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".json")


def record_success(config, output, cwd, args, info, decision):
    # Reuse and compilation have already succeeded; diagnostics and future validation are optional.
    try:
        record(config, decision)
    except Exception as error:
        print(f"GPU program {output.name}: could not record the cache decision ({error}).", flush=True)
    if info is None:
        return
    try:
        build = Path(config["build"])
        if output.parent.parent != build / "cycles/src/kernel/device":
            return
        path = validation_file(build, output)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".partial")
        partial.write_bytes(canonical({"args": args, "cwd": cwd, "key": info["key"], "sha256": sha256_file(output)}))
        partial.replace(path)
    except Exception as error:
        print(f"GPU program {output.name}: could not record the build inputs ({error}).", flush=True)


def invalidate_outputs(build, config=None):
    # Ninja watches source paths and command lines, so changing a compiler or include search
    # in place may never call the wrapper. Compare the previous invocation against today's
    # effective inputs before Ninja decides whether to run it, and remove only a stale output.
    removed = []
    for output in verified_outputs(build):
        if config:
            try:
                stamp = json.loads(validation_file(build, output).read_text(encoding="utf-8"))
                target, _ = kernel_compile(stamp["args"])
                recorded_output = Path(stamp["cwd"], target).resolve()
                if recorded_output == output.resolve() and sha256_file(output) == stamp["sha256"]:
                    with scratch_folder(config) as scratch:
                        current = compute_key(config, stamp["args"], stamp["cwd"], dict(os.environ), Path(scratch))
                    if current["key"] == stamp["key"]:
                        continue
            except Exception:
                pass  # A missing or unreadable input requires a genuine rebuild.
        output.unlink()
        removed.append(output.name)
    return removed


def wrapper(config_path: str, args: list[str], fallback_nvcc: str | None = None) -> int:
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        nvcc = fallback_nvcc or config["nvcc"]
    except (OSError, ValueError, KeyError, TypeError):
        if fallback_nvcc is None:
            raise
        return run_nvcc(fallback_nvcc, args)
    target = kernel_compile(args)
    if not target:
        return run_nvcc(nvcc, args)
    cwd = os.getcwd()
    output = Path(cwd, target[0]).resolve()
    name = output.name
    started = time.time()
    try:
        with scratch_folder(config) as scratch:
            info = compute_key(config, args, cwd, dict(os.environ), Path(scratch))
        stored, why = read_entry(Path(config["cache"]), info["key"], name)
    except Exception as error:  # Any doubt: compile as without this tool, store nothing.
        info, stored, why = None, None, "unusable"
        print(f"GPU program {name}: {REASONS['unusable']} ({error}).", flush=True)
    if stored is not None:
        try:
            place(stored[0], output, stored[1])
        except Exception as error:
            info, why = None, "unusable"
            print(f"GPU program {name}: the stored copy could not be used ({error}); compiling.", flush=True)
        else:
            print(f"GPU program {name}: reused ({REASONS['reused']}).", flush=True)
            record_success(
                config,
                output,
                cwd,
                args,
                info,
                {"output": name, "decision": "reused", "seconds": round(time.time() - started, 1), "key": info["key"]},
            )
            return 0
    compile_started = time.time()
    code = run_nvcc(nvcc, args)
    seconds = time.time() - compile_started
    stored_ok = False
    if code == 0 and info is not None and output.is_file():
        try:
            store_entry(Path(config["cache"]), info["key"], info["parts"], output, seconds)
            stored_ok = True
        except Exception as error:
            print(f"GPU program {name}: compiled, but could not be stored for reuse ({error}).", flush=True)
    if code == 0:
        record_success(
            config,
            output,
            cwd,
            args,
            info,
            {
                "output": name,
                "decision": "compiled",
                "why": why,
                "stored": stored_ok,
                "seconds": round(time.time() - started, 1),
                "compile_seconds": round(seconds, 1),
                "key": info["key"] if info else None,
            },
        )
    return code


def prepare(build: Path, cache: Path, nvcc: Path, cuda_root: Path, check_existing=False) -> dict:
    build, cache = Path(build).resolve(), Path(cache).resolve()
    folder = build / REUSE_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    if not Path(nvcc).is_file():
        raise SystemExit(f"nvcc not found: {nvcc}")
    config = {
        "schema": KEY_SCHEMA,
        "build": str(build),
        "cache": str(cache),
        "nvcc": str(nvcc),
        "cuda_root": str(cuda_root),
        "compiler": compiler_fingerprint(cuda_root),
    }
    invalidated = invalidate_outputs(build, config) if check_existing else []
    text = json.dumps(config, indent=1)
    partial = folder / "config.json.partial"
    partial.write_text(text, encoding="utf-8")
    os.replace(partial, folder / "config.json")
    script = Path(__file__).resolve()
    wrapper_file = folder / ("nvcc.cmd" if os.name == "nt" else "nvcc.sh")
    if os.name == "nt":
        body = f'@"{sys.executable}" -I "{script}" nvcc "{folder / "config.json"}" "{nvcc}" %*\r\n@exit /b %ERRORLEVEL%\r\n'
    else:
        body = f'#!/bin/sh\nexec "{sys.executable}" -I "{script}" nvcc "{folder / "config.json"}" "{nvcc}" "$@"\n'
    if not wrapper_file.is_file() or wrapper_file.read_text(encoding="utf-8") != body:
        with open(wrapper_file, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
        if os.name != "nt":
            wrapper_file.chmod(0o755)
    shutil.rmtree(folder / DECISIONS, ignore_errors=True)
    old = folder / "decisions.jsonl"  # the single shared file of the first version
    if old.exists():
        old.unlink()
    return {
        "wrapper": str(wrapper_file),
        "config": str(folder / "config.json"),
        "compiler_files": config["compiler"]["files"],
        "invalidated": invalidated,
    }


def ninja_kernel_seconds(build: Path, offset: int):
    """Seconds from the first kernel compile's start to the last one's end in this ninja run (.ninja_log after offset)."""
    log = Path(build) / ".ninja_log"
    if not log.is_file():
        return None
    if log.stat().st_size < offset:  # ninja rewrote its log: this run's times cannot be told apart
        return None
    with open(log, "rb") as handle:
        handle.seek(max(0, offset))
        text = handle.read().decode("utf-8", "replace")
    spans = []
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) >= 4 and fields[0].isdigit() and fields[1].isdigit():
            path = fields[3].replace("\\", "/")
            if "/kernel/device/" in path and path.lower().endswith(KERNEL_SUFFIXES):
                spans.append((int(fields[0]), int(fields[1])))
    if not spans:
        return None
    return round((max(e for _, e in spans) - min(s for s, _ in spans)) / 1000.0, 1)


def minutes_seconds(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


def report(build: Path, offset: int, mode: str) -> dict:
    build = Path(build)
    folder = build / REUSE_FOLDER
    decisions = read_decisions(build) if mode == "on" else []
    span = ninja_kernel_seconds(build, offset)
    reused = sorted(d["output"] for d in decisions if d["decision"] == "reused")
    compiled = sorted(d["output"] for d in decisions if d["decision"] == "compiled")
    damaged = any(d.get("why") == "damaged" for d in decisions)
    unusable = any(d.get("why") == "unusable" for d in decisions)
    if mode == "on" and decisions and not compiled:
        line = "GPU programs reused from the first build: nothing they are made from changed."
        state = "reused"
    elif compiled or (mode != "on" and span is not None):
        state = "compiled"
        took = f" in {minutes_seconds(span)}" if span is not None else ""
        if mode != "on":
            line = f"GPU programs compiled{took} (reuse is turned off)."
        else:
            line = f"GPU programs compiled{took}"
            line += f" ({len(compiled)} compiled, {len(reused)} reused)" if reused else ""
            line += ": the next Houdini build reuses them."
            if damaged:
                line += " A damaged stored copy was found and ignored."
            if unusable:
                line += " The reuse check could not be done for some, so they were compiled as usual."
    else:
        state = "unchanged"
        line = "GPU programs: already up to date in this build folder."
    summary = {
        "schema": KEY_SCHEMA,
        "mode": mode,
        "state": state,
        "kernel_seconds": span,
        "reused": reused,
        "compiled": compiled,
        "damaged_ignored": damaged,
        "check_unusable": unusable,
        "decisions": decisions,
        "line": line,
        "finished_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / "last-build.json.partial"
    partial.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    os.replace(partial, folder / "last-build.json")
    return summary


def prune(cache: Path, keep: int = 2) -> list[str]:
    """Keeps the newest `keep` complete entries per kernel file name; removes older ones and leftovers."""
    cache = Path(cache)
    removed = []
    if not cache.is_dir():
        return removed
    groups = {}
    for folder in cache.iterdir():
        if not folder.is_dir():
            continue
        if folder.name.startswith(".partial-"):
            if time.time() - folder.stat().st_mtime > 6 * 3600:  # left over by a stopped build
                shutil.rmtree(folder, ignore_errors=True)
                removed.append(folder.name)
            continue
        try:
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            name = manifest["output"]
            used = (
                (folder / "last-used").stat().st_mtime
                if (folder / "last-used").exists()
                else (folder / "manifest.json").stat().st_mtime
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue  # judged (and removed) when it is looked up
        groups.setdefault(name, []).append((used, folder))
    for entries in groups.values():
        entries.sort(reverse=True)
        for _, folder in entries[keep:]:
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder.name)
    return removed


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "nvcc":
        if len(argv) < 3:
            raise SystemExit("usage: kernel-cache.py nvcc <config.json> <real nvcc> <nvcc arguments>")
        return wrapper(argv[1], argv[3:], argv[2])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--build", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--nvcc", required=True)
    p.add_argument("--cuda-root", required=True)
    p.add_argument("--check-existing", action="store_true")
    i = sub.add_parser("invalidate")
    i.add_argument("--build", required=True)
    r = sub.add_parser("report")
    r.add_argument("--build", required=True)
    r.add_argument("--ninja-log-offset", type=int, default=0)
    r.add_argument("--mode", choices=("on", "off"), default="on")
    q = sub.add_parser("prune")
    q.add_argument("--cache", required=True)
    q.add_argument("--keep", type=int, default=2)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        print(
            json.dumps(
                prepare(
                    Path(args.build),
                    Path(args.cache),
                    Path(args.nvcc),
                    Path(args.cuda_root),
                    check_existing=args.check_existing,
                )
            )
        )
    elif args.command == "invalidate":
        print(json.dumps({"invalidated": invalidate_outputs(Path(args.build))}))
    elif args.command == "report":
        print(report(Path(args.build), args.ninja_log_offset, args.mode)["line"])
    else:
        print(json.dumps({"removed": prune(Path(args.cache), args.keep)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
