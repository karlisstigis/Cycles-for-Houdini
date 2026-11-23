# SPDX-License-Identifier: Apache-2.0
"""Compare native layouts independently of Houdini's generated folder names."""

import hou


def native_layout(group):
    def stable(template, path):
        if isinstance(template, hou.FolderParmTemplate):
            template.setName("verified_folder_" + "_".join(map(str, path)))
            template.setParmTemplates(
                [stable(child, (*path, index)) for index, child in enumerate(template.parmTemplates())]
            )
        elif template.name() == "light_contribenable#":
            # Houdini 22.0.429 drops this cosmetic multiparm join flag even
            # when assigning its original group unchanged (independent no-op
            # probe in artifacts/tab-native-multiple-probe.py). Compare every
            # other native definition, layout flag and parameter name exactly.
            template.setJoinWithNext(False)
        return template

    return hou.ParmTemplateGroup(
        [stable(entry, (index,)) for index, entry in enumerate(group.entries())]
    ).asDialogScript()
