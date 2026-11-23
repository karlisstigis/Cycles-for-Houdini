# SPDX-License-Identifier: Apache-2.0
"""Pinned Blender public evaluators baked independently for stock Cycles."""

import struct
import json


def f32(value):
    return struct.unpack("f", struct.pack("f", value))[0]


def sampled_rgba(ramp, positions):
    table = [list(ramp.evaluate(i / 256)) for i in range(257)]
    interpolate = ramp.color_mode != "RGB" or ramp.interpolation != "CONSTANT"
    result = []
    for x in positions:
        index = f32(min(max(f32(x), 0), 1) * 256)
        i = int(index)
        weight = f32(index - i)
        result.append(
            table[i]
            if not interpolate or i == 256
            else [f32(f32(f32(1 - weight) * a) + f32(weight * b)) for a, b in zip(table[i], table[i + 1])]
        )
    return result


def write_tables(rows, path):
    references = {}
    for row in rows:
        references[row["id"]] = {
            key.removeprefix("table_"): row.pop(key) for key in ("table_colors", "table_alphas") if key in row
        }
    path.with_name(path.stem + "-tables.json").write_text(json.dumps(references, separators=(",", ":")) + "\n")
