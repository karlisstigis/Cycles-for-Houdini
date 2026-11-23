# SPDX-License-Identifier: Apache-2.0
"""Semantic JSON -> parameter templates without a Houdini installation."""

import json
import runpy
import sys
from types import SimpleNamespace

import pytest

from support.paths import ROOT

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


class Parm:
    def __init__(self, name, label="", **kwargs):
        self._name, self.label = name, label
        self.hidden = False
        self.join = False
        self.hide_label = False
        self.conditions = {}

    def name(self):
        return self._name

    def setLabel(self, label):
        self.label = label

    def setJoinWithNext(self, join):
        self.join = join

    def hideLabel(self, hide):
        self.hide_label = hide

    def hide(self, hide):
        self.hidden = hide

    def setConditional(self, kind, value):
        self.conditions[kind] = value

    def type(self):
        return "leaf"


class Folder(Parm):
    def __init__(self, name, label, children=(), folder_type="Tabs", **kwargs):
        super().__init__(name, label)
        self.children, self.kind, self.tags = tuple(children), folder_type, kwargs.get("tags", {})

    def parmTemplates(self):
        return self.children

    def folderType(self):
        return self.kind


class Group:
    def __init__(self, entries):
        self.values = list(entries)

    def entries(self):
        return tuple(self.values)

    def append(self, parm):
        self.values.append(parm)

    def find(self, name):
        return next((parm for parm in self.values if parm.name() == name), None)


def test_layout_authors_schema_and_retains_hidden_internal_parameters(monkeypatch):
    schema = json.loads((ROOT / "scripts/rop-layout.json").read_text())

    def leaves(entries):
        for entry in entries:
            if "folder" in entry:
                yield from leaves(entry["children"])
            elif not entry.get("separator"):
                yield Parm(entry["name"])

    original = Group(
        [*leaves(schema), Folder("device_gpu_list", "GPU", folder_type="MultiparmBlock"), Parm("retained_internal")]
    )
    hou = SimpleNamespace(
        FolderParmTemplate=Folder,
        SeparatorParmTemplate=Parm,
        LabelParmTemplate=Parm,
        ParmTemplateGroup=Group,
        folderType=SimpleNamespace(
            Simple="Simple", Tabs="Tabs", Collapsible="Collapsible", MultiparmBlock="MultiparmBlock"
        ),
        parmTemplateType=SimpleNamespace(Separator="separator"),
        parmCondType=SimpleNamespace(HideWhen="hide"),
    )
    monkeypatch.setitem(sys.modules, "hou", hou)
    apply = runpy.run_path(str(ROOT / "scripts/rop-layout.py"))["apply"]
    result = apply(original)
    assert [p.label for p in result.entries() if isinstance(p, Folder)] == [
        "Common Settings",
        "Rendering",
        "Scene",
        "Image Output",
        "Performance",
        "Devices",
        "Scripts",
    ]
    assert result.find("execute").join
    assert result.find("renderdialog").hide_label
    internal = result.find("retained_internal")
    assert internal is original.find("retained_internal") and internal.hidden
    assert internal.conditions == {"hide": ""}
    devices = next(p for p in result.entries() if p.name() == "ch_devices")
    assert sum(p.name() == "device_gpu_list" for p in devices.parmTemplates()) == 1
    assert result.find("device_gpu_list") is None

    def descendants(entries):
        for parm in entries:
            yield parm
            if isinstance(parm, Folder):
                yield from descendants(parm.parmTemplates())

    authored = {parm.name(): parm for parm in descendants(result.entries())}
    for name, toggle, children in (
        ("ch_performance_tiling", "use_tiling", ("tile_size",)),
        (
            "ch_performance_texture_cache",
            "use_texture_cache",
            ("auto_texture_cache", "texture_cache_generate", "texture_cache_clear"),
        ),
        (
            "ch_image_output_film_transparency",
            "transparent_background",
            ("transparent_glass", "transparent_roughness_threshold"),
        ),
    ):
        folder = authored[name]
        assert folder.folderType() == "Collapsible"
        assert folder.tags["sidefx::header_toggle"] == toggle
        assert tuple(parm.name() for parm in folder.parmTemplates()) == children
    assert authored["texture_cache_generate"].join
    assert authored["auto_texture_cache"].label == "Auto Generate"
    assert "label_texture_cache" not in authored
