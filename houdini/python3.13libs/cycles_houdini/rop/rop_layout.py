# SPDX-License-Identifier: Apache-2.0
"""Keep saved per-node ROP controls aligned with the current layout."""


def ensure_ipr_settings_layout(node):
    """Move existing controls atomically; Houdini retains their channels by name.

    Device selection authors per-node layouts, which can keep the previous
    folder positions after an HDA update. Do not replace the full layout or
    reset user parameters. New definitions already have this arrangement.
    """
    group = node.parmTemplateGroup()
    changed = False
    definition = node.type().definition()
    if definition is not None:
        action = group.find("ch_render_mplay")
        current_action = definition.parmTemplateGroup().find("ch_render_mplay")
        if action is not None and current_action is not None and "Output Picture is not written" in action.help():
            action.setHelp(current_action.help())
            group.replace("ch_render_mplay", action)
            changed = True
    if definition is not None and len(group.findIndices("execute")) == 1:
        defaults = definition.parmTemplateGroup()
        for name, anchor, before in (("ch_ipr", "execute", True), ("ch_render_mplay", "execute", False)):
            template = defaults.find(name)
            indices = group.findIndices(name)
            anchor_indices = group.findIndices(anchor)
            desired = anchor_indices[0] + (-1 if before else 1)
            if template is not None and (not indices or (len(indices) == 1 and indices[0] != desired)):
                # Houdini appends newly inherited HDA controls to saved custom
                # layouts, even when the old layout omitted them entirely.
                if indices:
                    group.remove(indices)
                if before:
                    group.insertBefore(anchor, template)
                else:
                    group.insertAfter(anchor, template)
                changed = True
        if changed:
            for name in ("ch_ipr", "execute", "ch_render_mplay"):
                template = group.find(name)
                if template is not None:
                    template.setJoinWithNext(True)
                    group.replace(name, template)
            controls = group.find("ch_controls")
            if controls is not None and group.find("ch_render_mplay") is not None:
                group.remove("ch_controls")
                group.insertAfter("ch_render_mplay", controls)
    take_indices = group.findIndices("take")
    scene_indices = group.findIndicesForFolder(("Scene",))
    frame_indices = group.findIndices("f")
    if take_indices and scene_indices and take_indices[:-1] == scene_indices and len(frame_indices) == 1:
        take = group.entryAtIndices(take_indices)
        group.remove(take_indices)
        group.insertAfter("f", take)
        changed = True
    adaptive = group.find("tessellation_update")
    if adaptive and adaptive.label() == "Tessellation Update":
        adaptive.setLabel("Adaptive Subdivision Update")
        group.replace("tessellation_update", adaptive)
        changed = True
    # Houdini normalizes folder-set names when instantiating the HDA. Locate
    # the section through its parameter and the destination through UI labels.
    source_indices = group.findIndices("ipr_samples")[:-1]
    path = ("Rendering", "IPR Settings")
    destination_indices = group.findIndicesForFolder(path)
    legacy_indices = group.findIndicesForFolder(("Rendering", "Sampling"))
    if source_indices and destination_indices and legacy_indices and source_indices[:-1] == legacy_indices:
        source = group.entryAtIndices(source_indices)
        if source.label() == "Interactive Render":
            source.setLabel("Sampling")
            source.setTags(dict(source.tags(), group_default="1"))
            group.remove(source_indices)
            destination_indices = group.findIndicesForFolder(path)
            destination = group.entryAtIndices(destination_indices)
            destination.setParmTemplates((source, *destination.parmTemplates()))
            group.replace(destination_indices, destination)
            changed = True
    if changed:
        node.setParmTemplateGroup(group)
    return changed
