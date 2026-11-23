# SPDX-License-Identifier: Apache-2.0
"""K32 first-output/store ablation using the frozen matched-reference link recipes.

Run from a Visual Studio developer shell. This builds standalone diagnostics,
never a production library or installed worker. Requires the phase2-decisions
light-tree evidence and its original library dependencies on this workstation.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess


ROOT = Path(__file__).resolve().parents[1]
ARMS = ("reference", "production", "skip-store")
EDITS = ("cold", "cold-move", "full-palette", "full-palette-move")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute(command, log, env):
    with log.open("wb") as stream:
        subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=True,
            timeout=300,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=ROOT / "artifacts/phase2-decisions")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--resources", type=Path, required=True, help="Kernel resources matching the linked current Cycles libraries"
    )
    parser.add_argument("--device", choices=("CPU", "OptiX"), default="CPU")
    parser.add_argument("--triples", type=int, default=3)
    args = parser.parse_args()
    if args.triples < 3:
        parser.error("At least three rotated triples are required")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / (args.device + ".json")).exists():
        raise FileExistsError("Refusing to replace completed evidence")
    old = args.evidence.resolve() / "light-tree"
    k32 = args.evidence.resolve() / "k32"
    cache_source = ROOT / "third_party/cycles/src/scene/light_tree_cache.cpp"
    omission_source = (k32 / "store_probe.cpp").read_text()
    assert omission_source.replace('  if (std::getenv("K32_SKIP_STORE")) return;\n', "") == cache_source.read_text()
    resources = args.resources.resolve()
    assert (resources / "lib/kernel_optix.ptx.zst").is_file()
    env = {
        key: value
        for key, value in os.environ.items()
        if not (key.startswith("CYCLES_HOUDINI_") and "LIGHT" in key) and key != "K32_SKIP_STORE"
    }
    hfs = Path("C:/Program Files/Side Effects Software/Houdini 22.0.466")
    bundle = ROOT / "third_party/cycles/lib/windows_x64"
    env["PATH"] = (
        os.pathsep.join(map(str, [hfs / "bin", hfs / "custom/houdini/dsolib", *bundle.glob("*/bin")]))
        + os.pathsep
        + env["PATH"]
    )
    env.update(
        K32_DEVICE="CPU" if args.device == "CPU" else "CUDA_NVIDIA GeForce RTX 4090_0000:03:00_OptiX",
        K32_RESOURCES=str(resources),
        K32_OUTPUT=str(output),
    )
    source = ROOT / "tests/light_tree_first_frame_benchmark.cpp"
    fixture = output / "fixture.obj"
    command = (old / "bin/patched/engine_benchmark-compile.txt").read_text()
    assert "-DWITH_IMAGE_IRIS" in command
    command = re.sub(r'/Fo"[^"]+"', f'/Fo"{fixture.as_posix()}"', command)
    command = re.sub(r'/Fd"[^"]+"', f'/Fd"{output.as_posix()}/"', command)
    command = command[: command.rindex(" -c ")] + f' -c "{source}"'
    execute(command, output / "compile.log", env)
    for arm in ARMS:
        recipe = "reference" if arm == "reference" else "patched"
        command = (old / f"bin/{recipe}/link.txt").read_text()
        command = re.sub(r'/OUT:"[^"]+"', f'/OUT:"{(output / (arm + ".exe")).as_posix()}"', command)
        command = re.sub(r'/MAP:"[^"]+"', f'/MAP:"{(output / (arm + ".map")).as_posix()}"', command)
        objects = f'"{fixture.as_posix()}"'
        if arm == "skip-store":
            # The earlier store-only probe overrides one archive symbol. Its
            # environment switch bypasses retention, without deferring work.
            objects += f' "{(k32 / "store_probe.obj").as_posix()}"'
        command = re.sub(r'"[^"]+engine_benchmark\.obj"', lambda match: objects, command)
        execute(command, output / (arm + "-link.log"), env)

    def capture(arm, prefix, fresh=0):
        environment = dict(env)
        if arm == "skip-store":
            environment["K32_SKIP_STORE"] = "1"
        command = [str(output / (arm + ".exe")), "mixed", str(prefix), str(fresh), "1"]
        execute(command, prefix.with_suffix(".log"), environment)
        lines = prefix.with_suffix(".log").read_text().splitlines()
        rows = [json.loads(line[8:]) for line in lines if line.startswith("MEASURE ")]
        assert len(rows) == (1 if fresh else 4)
        assert all(0 <= row["first_frame_ms"] <= row["ms"] for row in rows)
        assert any("threads 0 " in line for line in lines if line.startswith("K32 "))
        return {
            row["edit"]: dict(
                first_frame_ms=row["first_frame_ms"],
                complete_ms=row["ms"],
                image_sha256=digest(prefix.with_name(prefix.name + "-" + row["edit"] + ".f32")),
            )
            for row in rows
        }

    reference = {}
    for state, edit in enumerate(EDITS, 1):
        reference.update(capture("reference", output / (args.device + "-fresh-" + edit), state))
    triples = []
    for index in range(args.triples):
        order = ARMS[index % 3 :] + ARMS[: index % 3]
        trial = {}
        for arm in order:
            trial[arm] = capture(arm, output / f"{args.device}-{index}-{arm}")
            for edit in EDITS:
                assert trial[arm][edit]["image_sha256"] == reference[edit]["image_sha256"], (index, arm, edit)
        triples.append(dict(order=order, arms=trial))
        print(args.device, "triple", index + 1, "passed", flush=True)
    medians = {
        arm: {
            edit: {
                metric: statistics.median(trial["arms"][arm][edit][metric] for trial in triples)
                for metric in ("first_frame_ms", "complete_ms")
            }
            for edit in EDITS
        }
        for arm in ARMS
    }
    result = dict(
        result="passed",
        device=env["K32_DEVICE"],
        threads=0,
        medians_ms=medians,
        fresh_references=reference,
        triples=triples,
        scope="Edit submission to first complete one-sample output callback and session completion; no GUI paint",
        comparator="Matched stock tree with palette semantics; skip-store is an omission ablation, not deferred storage",
        fixture_sha256=digest(source),
        binaries_sha256={arm: digest(output / (arm + ".exe")) for arm in ARMS},
        store_probe_object_sha256=digest(k32 / "store_probe.obj"),
        cache_source_sha256=digest(cache_source),
        resource_root=str(resources),
        optix_kernel_sha256=digest(resources / "lib/kernel_optix.ptx.zst"),
        source_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    )
    (output / (args.device + ".json")).write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
