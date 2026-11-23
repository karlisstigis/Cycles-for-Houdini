# SPDX-License-Identifier: Apache-2.0
"""Judges for the fresh-clone checks: pass or fail, with a reason, from recorded states only.

The checks (scripts/fresh-clone-community-check.ps1 and the PN drivers) DO the long steps (builds, installs) and
record what they find as JSON; this file JUDGES those records. It runs nothing and reads no build folder, so every
judgement can be tested in seconds against the records of earlier runs (tests/community_build_test.py
FreshCloneJudgeTest, fixtures in tests/fixtures/fresh_clone_judge/).

  python -I scripts/fresh-clone-judge.py <judge> <arguments.json>   # prints {"ok": true|false, "reason": "..."}
  python -I scripts/fresh-clone-judge.py long-root <arguments.json>  # prints {"root": "..."} (see long_root)

A kernel state (the check's Get-KernelState) has: state, mode, reused, compiled, damaged_ignored, line, decisions
([{output, decision, why}]) and files ({"cuda/<name>": sha256, "optix/<name>": sha256}, 12 entries with .zst).

What ninja runs (the engine's CMake files: src/kernel/CMakeLists.txt, device/cuda and device/optix; checked with
`ninja -t query` on a real build folder: 225 inputs per CUDA rule, 226 per OptiX rule),
which decides what can be "reused" or "compiled":
  - a CUDA kernel rule (kernel_compute_75.ptx, kernel_sm_86.cubin, kernel_sm_89.cubin) depends on
    device/cuda/kernel.cu and the kernel headers (types.h among them); not on another kernel's output;
  - an OptiX kernel rule depends on its own device/optix source, the OptiX device headers and the kernel headers
    (types.h among them), NOT on device/cuda/kernel.cu;
  - each kernel's .zst depends on that kernel only;
  - a rule runs when one of its inputs or its command changed (the switch -NoKernelReuse changes the command of
    all six; a new kernel-cache.py does not: it is not a ninja input);
  - kernel-cache.py report() lists only decisions of rules that ran: a kernel whose rule did not run is neither
    "reused" nor "compiled".
"""

from __future__ import annotations

import json
import sys

CUDA = ("kernel_compute_75.ptx", "kernel_sm_86.cubin", "kernel_sm_89.cubin")
OPTIX = ("kernel_optix.ptx", "kernel_optix_mnee.ptx", "kernel_optix_shader_raytrace.ptx")
ALL = CUDA + OPTIX
FILES = 12  # six kernels and their .zst copies


def ok(reason="passed"):
    return {"ok": True, "reason": reason}


def fail(reason):
    return {"ok": False, "reason": reason}


def _list(value):
    """A list as written by PowerShell: also {"value": [...], "Count": n} (Windows PowerShell 5.1's form of an array
    that came from ConvertFrom-Json, when System.Array's extended type is not removed) and one bare string."""
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    if isinstance(value, str):
        return [value]
    return list(value or [])


def _names(state, key):
    return sorted(set(_list(state.get(key))))


def _ran(state):
    return sorted(set(_names(state, "reused")) | set(_names(state, "compiled")))


def _nothing_ran(state):
    return not _ran(state) and not _list(state.get("decisions"))


def _same_files(state, reference, what):
    files, ref = state.get("files") or {}, reference.get("files") or {}
    if len(files) != FILES:
        return f"{what}: expected {FILES} kernel files, found {len(files)}"
    if files != ref:
        differ = sorted(k for k in set(files) | set(ref) if files.get(k) != ref.get(k))
        return f"{what}: the kernel files differ ({', '.join(differ)})"
    return None


def _line(state):
    return state.get("line") or "no report line"


def first_compile(state):
    """Reuse on, nothing stored for these inputs: the stand-in compiles all six."""
    if state.get("mode") != "on":
        return fail(f"reuse is not on ({state.get('mode')})")
    if _nothing_ran(state):
        return fail(f"no kernel rule ran (ninja found them up to date): {_line(state)}")
    if _names(state, "compiled") != sorted(ALL):
        return fail(f"expected all six compiled, got compiled {_names(state, 'compiled')}, reused {_names(state, 'reused')}")
    return ok()


def reuse_all(state, reference):
    """Reuse on, every rule ran and every kernel came from the store, byte-identical to the reference."""
    if state.get("mode") != "on":
        return fail(f"reuse is not on ({state.get('mode')})")
    if _nothing_ran(state):
        return fail(f"no kernel rule ran (ninja found them up to date): {_line(state)}")
    if _names(state, "compiled"):
        return fail(f"compiled {_names(state, 'compiled')} although nothing they are made from changed: {_line(state)}")
    if _names(state, "reused") != sorted(ALL):
        missing = sorted(set(ALL) - set(_names(state, "reused")))
        return fail(f"reused {len(_names(state, 'reused'))} of six (missing: {', '.join(missing)}): {_line(state)}")
    problem = _same_files(state, reference, "reused")
    return fail(problem) if problem else ok()


def kept(state, reference):
    """An update with unchanged kernel inputs: nothing compiled, files as before (rules may not even run)."""
    if _names(state, "compiled"):
        return fail(f"compiled {_names(state, 'compiled')} although nothing they are made from changed: {_line(state)}")
    problem = _same_files(state, reference, "kept")
    return fail(problem) if problem else ok()


def reference_compile(state, references):
    """-NoKernelReuse: nvcc itself compiles all six (no stand-in, no decisions); identical to every reference."""
    if state.get("mode") != "off":
        return fail(f"reuse is not off ({state.get('mode')})")
    if _list(state.get("decisions")):
        return fail("the stand-in made decisions although reuse is off")
    if state.get("state") != "compiled":
        return fail(f"the kernels were not compiled: {_line(state)}")
    for name, reference in references.items():
        problem = _same_files(state, reference, f"reuse off vs {name}")
        if problem:
            return fail(problem)
    return ok()


def new_key_compiles_all(state):
    """A change in a file every kernel includes (a comment counts: inputs are keyed by content): all six compile."""
    if _nothing_ran(state):
        return fail(f"no kernel rule ran (ninja found them up to date): {_line(state)}")
    if _names(state, "compiled") != sorted(ALL):
        return fail(f"expected all six compiled, got {_names(state, 'compiled')}: {_line(state)}")
    return ok()


def real_edit(states, newest, before):
    """An edit of device/cuda/kernel.cu. Newest build: ninja runs the three CUDA rules only (the OptiX rules do not
    depend on that file), so compiled is exactly the three CUDA kernels and no OptiX kernel is compiled; the OptiX
    files stay byte-identical, the CUDA ones change. The other builds (their kernel headers changed since their last
    build, so all six rules run there): all six reused, identical to the newest."""
    n = states[newest]
    if _names(n, "compiled") != sorted(CUDA):
        return fail(f"newest: expected the three CUDA kernels compiled, got {_names(n, 'compiled')}: {_line(n)}")
    if set(_names(n, "compiled")) & set(OPTIX):
        return fail("newest: an OptiX kernel was compiled")
    files, old = n.get("files") or {}, before.get("files") or {}
    if len(files) != FILES:
        return fail(f"newest: expected {FILES} kernel files, found {len(files)}")
    for name in sorted(files):
        changed = files.get(name) != old.get(name)
        if name.startswith("cuda/") != changed:
            return fail(f"newest: {name} {'changed' if changed else 'did not change'}; only the CUDA kernels may change")
    for version, state in states.items():
        if version == newest:
            continue
        result = reuse_all(state, n)
        if not result["ok"]:
            return fail(f"Houdini {version}: {result['reason']}")
    return ok()


def damaged(state, name, reference):
    """One stored copy damaged and that kernel removed from the build folder: the copy is detected, the kernel is
    compiled again (later archs in the chain run too and may be reused) and the result equals the reference."""
    if not state.get("damaged_ignored"):
        return fail(f"the damaged copy was not detected: {_line(state)}")
    decision = next((d for d in _list(state.get("decisions")) if d.get("output") == name), None)
    if not decision or decision.get("decision") != "compiled" or decision.get("why") != "damaged":
        return fail(f"{name}: expected compiled because the stored copy was damaged, got {decision}")
    problem = _same_files(state, reference, "after the damaged copy")
    return fail(problem) if problem else ok()


def cache_entries(counts):
    if not counts:
        return fail("no stored kernels")
    over = {k: v for k, v in counts.items() if v > 2}
    return fail(f"more than two stored copies: {over}") if over else ok()


# Installed path length (Task Y) -------------------------------------------------------------------------------
LIMIT = 259


def long_root(base, below_root, total, separator="\\"):
    """An installation root <base>\\<padding>\\installation such that root + separator + below_root == total.
    below_root is the length of the longest path below the root (versions\\<16 hex>\\<longest package path>)."""
    fixed = len(base) + 1 + 1 + len("installation") + 1 + below_root  # base \ pad \ installation \ below
    pad = total - fixed
    if pad < 1:
        raise ValueError(f"the base folder {base} is too long for a total of {total}")
    root = base + separator + "r" * pad + separator + "installation"
    assert len(root) + 1 + below_root == total
    return {"root": root, "padding": pad}


def path_rounds(record):
    """The check's Setup-style rounds with long app folders (rounds.path_length)."""
    if record.get("longest_in_package_length", 999) > 86:
        return fail(f"a package path has {record.get('longest_in_package_length')} characters (budget 86)")
    if record.get("below_app_folder", 999) > 125:
        return fail(f"{record.get('below_app_folder')} characters below the app folder (budget 125)")
    over, at = record.get("one_over_the_limit") or {}, record.get("at_the_limit") or {}
    for name, case, total in (("one over the limit", over, LIMIT + 1), ("at the limit", at, LIMIT)):
        if "root_plus_below" in case and case["root_plus_below"] != total:
            return fail(f"{name}: the check's folder gives {case['root_plus_below']} characters, not {total}")
    if over.get("result") != "refused" or "260 characters" not in (over.get("message") or ""):
        return fail(f"one character over the limit: {over.get('result')}: {over.get('message')}")
    if over.get("created_app_folder") or over.get("wrote_preferences"):
        return fail("the refused install wrote something (its app folder or the preferences)")
    if not at.get("result", "").startswith("installed"):
        return fail(f"at the limit: {at.get('result')}: {at.get('message')}")
    if at.get("longest_installed") != LIMIT:
        return fail(f"at the limit the longest installed file has {at.get('longest_installed')} characters, not {LIMIT}")
    if over.get("app_folder_length") != at.get("app_folder_length", -1) + 1:
        return fail("the two folders are not one character apart")
    return ok()


def listing_changes(before, after):
    """Entries in only one of two listings (either may be missing or empty)."""
    old, new = set(before or []), set(after or [])
    return sorted(new - old) + sorted(old - new)


def nothing_outside(before, after):
    changes = listing_changes(before, after)
    return fail(f"{len(changes)} change(s) outside the project: {changes[:5]}") if changes else ok()


JUDGES = {
    "first-compile": first_compile,
    "reuse-all": reuse_all,
    "kept": kept,
    "reference-compile": reference_compile,
    "new-key-compiles-all": new_key_compiles_all,
    "real-edit": real_edit,
    "damaged": damaged,
    "cache-entries": cache_entries,
    "path-rounds": path_rounds,
    "nothing-outside": nothing_outside,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    name, path = argv
    with open(path, encoding="utf-8-sig") as handle:
        arguments = json.load(handle)
    if name == "long-root":
        print(json.dumps(long_root(**arguments)))
        return 0
    print(json.dumps(JUDGES[name](**arguments)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
