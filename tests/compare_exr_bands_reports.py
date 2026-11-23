# SPDX-License-Identifier: Apache-2.0
"""Compare independently verified decoded EXR hashes and encode measurements."""

import argparse
import json
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("before", type=Path)
p.add_argument("after", type=Path)
p.add_argument("--report", type=Path, required=True)
args = p.parse_args()
before = json.loads(args.before.read_text())
after = json.loads(args.after.read_text())
errors = []
runs = []
if before["result"] != "passed" or after["result"] != "passed":
    errors.append("Input verification did not pass")
if (before["width"], before["height"]) != (after["width"], after["height"]):
    errors.append("Dimensions differ")
old = {run["target"]: run for run in before["runs"]}
new = {run["target"]: run for run in after["runs"]}
if set(old) != set(new):
    errors.append("Output color targets differ")
for target in sorted(set(old) & set(new)):
    a, b = old[target], new[target]
    same_pixels = a["decoded_sha256"] == b["decoded_sha256"]
    same_channels = a["channels"] == b["channels"]
    same_crypto = a["crypto"] == b["crypto"]
    if not (same_pixels and same_channels and same_crypto):
        errors.append("Decoded pixels/channels/crypto differ: " + target)

    def measurements(run):
        return dict(
            seconds=run["seconds"],
            peak_sampled_working_set=run["peak_sampled_working_set"],
            encode_working_set_growth=run["peak_sampled_working_set"] - run["working_set_before"],
            peak_sampled_private_commit=run["peak_sampled_private_commit"],
            encode_private_commit_growth=run["peak_sampled_private_commit"] - run["private_commit_before"],
            file_bytes=run["file_bytes"],
        )

    baseline, candidate = measurements(a), measurements(b)
    runs.append(
        dict(
            target=target,
            channels=len(b["channels"]),
            decoded_pixels_bit_identical=same_pixels,
            channel_layout_identical=same_channels,
            cryptomatte_metadata_identical=same_crypto,
            decoded_sha256=b["decoded_sha256"],
            baseline=baseline,
            candidate=candidate,
            encode_seconds_change_percent=100 * (b["seconds"] / a["seconds"] - 1),
            encode_working_set_growth_reduction_bytes=baseline["encode_working_set_growth"]
            - candidate["encode_working_set_growth"],
            encode_private_commit_growth_reduction_bytes=baseline["encode_private_commit_growth"]
            - candidate["encode_private_commit_growth"],
        )
    )
report = dict(
    result="failed" if errors else "passed",
    width=after["width"],
    height=after["height"],
    verification="SHA-256 of all decoded float pixel bits in every 32-row band, all channel names and Cryptomatte metadata; both runs independently validated against retained source planes and OCIO",
    measurement_scope="Sampled whole-process working set/private commit during save; retained render and codec buffers remain included; source-plane copies/independent OCIO validation follow measurement",
    before=str(args.before.absolute()),
    after=str(args.after.absolute()),
    runs=runs,
    errors=errors,
)
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, indent=2))
print(args.report)
if errors:
    raise SystemExit(1)
