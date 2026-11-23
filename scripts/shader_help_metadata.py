# SPDX-License-Identifier: Apache-2.0
"""Read the authored material VOP table, including adjacent C++ string literals."""

import json
import re


def shader_help_metadata(source):
    literal = r'"(?:[^"\\]|\\.)*"'
    field = rf"((?:{literal}\s*)+)"
    row = rf"\{{\s*{field},\s*\{{\s*{field},\s*{field},\s*{field}\}}\s*\}}"

    def text(field_value):
        return "".join(json.loads(token) for token in re.findall(literal, field_value))

    result = {
        text(kind): (text(label), text(category), text(summary))
        for kind, label, category, summary in re.findall(row, source)
    }
    ramp = source.partition("void register_color_ramp(")[2]
    summary = re.search(rf"data->help\s*=\s*{field};", ramp)
    if not result or summary is None:
        raise ValueError("Missing authored VOP table or Color Ramp synopsis in material_vop.cpp")
    result["color_ramp"] = ("Color Ramp", "Converter", text(summary.group(1)))
    return result
