# SPDX-License-Identifier: Apache-2.0
"""Author the current Cycles ROP layout from its semantic UI description."""

import json
from pathlib import Path
import hou


def apply(group):
    used = set()

    def items(spec):
        result = []
        for entry in spec:
            name = entry["name"]
            if "folder" in entry:
                used.add(name)
                tags = dict(entry.get("tags", {}))
                children = items(entry["children"])
                if entry["label"] == "Devices":
                    children.append(group.find("device_gpu_list"))
                    used.add("device_gpu_list")
                p = hou.FolderParmTemplate(
                    name, entry["label"], children, folder_type=getattr(hou.folderType, entry["folder"]), tags=tags
                )
            elif entry.get("separator"):
                p = hou.SeparatorParmTemplate(name, tags=dict(entry.get("tags", {})))
            else:
                p = group.find(name)
                if p is None:
                    raise RuntimeError("Unknown ROP layout parameter: " + name)
                used.add(name)
                p.setLabel(entry["label"])
                p.setJoinWithNext(entry.get("join", False))
                p.hideLabel(entry.get("hide_label", False))
            result.append(p)
        return result

    layout = hou.ParmTemplateGroup(items(json.loads(Path(__file__).with_name("rop-layout.json").read_text())))

    # Keep supported internal leaf controls outside the visible UI. Empty UI
    # containers carry no renderer data and are not serialized.
    def retain(entries):
        for p in entries:
            if isinstance(p, hou.FolderParmTemplate):
                if p.folderType() == hou.folderType.MultiparmBlock:
                    if p.name() not in used:
                        p.hide(True)
                        layout.append(p)
                else:
                    retain(p.parmTemplates())
            elif p.name() not in used and p.type() != hou.parmTemplateType.Separator:
                p.setConditional(hou.parmCondType.HideWhen, "")
                p.hide(True)
                layout.append(p)

    retain(group.entries())
    return layout
