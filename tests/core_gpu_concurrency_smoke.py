# SPDX-License-Identifier: Apache-2.0
"""Production GPU queue budget calculation, explicit overrides and isolated device policies; no GPU allocation."""

from native_test_runtime import runtime_executable
import json, os, subprocess


def main():
    executable = runtime_executable("ch_harness.exe")
    cases = {
        "default": {},
        "grow-override": {"GROW_PERCENT": "25"},
        "bounds-override": {
            "BASELINE": "393216",
            "MIN": "196608",
            "MAX": "458752",
            "RESERVE_PERCENT": "0",
            "FACTOR": "0.5",
        },
    }
    reports = []
    for mode, overrides in cases.items():
        env = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("CYCLES_CONCURRENT_STATES_")
        }
        env.update({"CYCLES_CONCURRENT_STATES_" + key: value for key, value in overrides.items()})
        run = subprocess.run(
            [str(executable), "--gpu-concurrency-contracts", mode], env=env, text=True, capture_output=True, timeout=30
        )
        assert run.returncode == 0, (mode, run.stdout, run.stderr)
        result = [json.loads(line) for line in run.stdout.splitlines() if line.startswith("{")][-1]
        assert result["result"] == "passed" and result["checks"] >= 9, result
        reports.append(result)
    print(json.dumps({"result": "passed", "gpu_concurrency": reports}))


if __name__ == "__main__":
    main()
