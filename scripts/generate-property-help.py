# SPDX-License-Identifier: Apache-2.0
"""Append the staged build's Cycles parameter reference to its OBJ guides."""

from pathlib import Path
import sys

import hou
from cycles_houdini import camera_properties, light_properties, object_properties


def reference(folder):
    lines = []

    def visit(entries, path):
        for parm in entries:
            if parm.isHidden():
                continue
            if isinstance(parm, hou.FolderParmTemplate):
                visit(parm.parmTemplates(), [*path, parm.label()])
            elif parm.type() not in (hou.parmTemplateType.Separator, hou.parmTemplateType.Label):
                description = parm.help().strip()
                if not description:
                    raise ValueError("Missing Cycles property help for " + parm.name())
                name = " / ".join([*path, parm.label()])
                lines.extend([name + ":", "    #id: " + parm.name(), "    " + description.replace("\n", "\n    "), ""])

    visit(folder.parmTemplates(), [])
    return "\n".join(lines)


def publish(name, sections):
    source = Path(__file__).resolve().parents[1] / "houdini/help/cycles" / (name + ".txt")
    before, related, after = source.read_text(encoding="utf-8").partition("@related")
    blocks = [before.rstrip(), "@parameters"]
    for folder in sections:
        blocks.append(reference(folder))
    if related:
        blocks.append(related + after)
    (output / "cycles" / source.name).write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


output = Path(sys.argv[1])
publish("objects", [object_properties.template()])
cam = hou.node("/obj").createNode("cam")
try:
    camera_properties.install(cam)
    camera_tab = next(p for p in cam.parmTemplateGroup().entries() if p.tags().get("cycles_camera_properties_version"))
    publish("camera", [camera_tab])
finally:
    cam.destroy()
publish("lights", [light_properties.template(False)])
publish("environment", [light_properties.template(True)])
