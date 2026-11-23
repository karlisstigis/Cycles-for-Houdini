# SPDX-License-Identifier: Apache-2.0
"""A Blender material that cannot translate never costs a reference its geometry, hull or render."""

import contextlib
import importlib
import itertools
import sys
import threading
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


class FakeNode:
    _ids = itertools.count(1)

    def __init__(self, registry, name):
        self.registry, self.name, self.ident, self.destroyed = registry, name, next(self._ids), False
        registry[self.ident] = self
        self.children = []

    def sessionId(self):
        return self.ident

    def createNode(self, kind, name, **unused):
        child = FakeNode(self.registry, name)
        self.children.append(child)
        return child

    def destroy(self):
        self.destroyed = True
        self.registry.pop(self.ident, None)


@pytest.fixture
def assets(monkeypatch, tmp_path):
    registry = {}
    fake_hou = SimpleNamespace(
        nodeBySessionId=registry.get,
        undos=SimpleNamespace(disabler=contextlib.nullcontext),
        text=SimpleNamespace(encodeAttrib=lambda value: value),
        hipFile=SimpleNamespace(eventCallbacks=lambda: [], addEventCallback=lambda callback: None),
    )
    monkeypatch.setitem(sys.modules, "hou", fake_hou)
    package = importlib.import_module("cycles_houdini.blender")
    previous = getattr(package, "blender_assets", None)
    module = importlib.import_module("cycles_houdini.blender.blender_assets")
    root = FakeNode(registry, "cycles_blend_prototypes")
    for name, value in dict(
        hou=fake_hou,
        _directory=SimpleNamespace(name=str(tmp_path)),
        _materials={},
        _network=lambda: root,
        _native_mesh=lambda mesh, arrays, identities, count: ("geometry/" + mesh["key"], "capsule"),
    ).items():
        monkeypatch.setattr(module, name, value)
    try:
        yield SimpleNamespace(module=module, root=root, registry=registry)
    finally:
        if previous is None:
            sys.modules.pop("cycles_houdini.blender.blender_assets", None)
            if hasattr(package, "blender_assets"):
                delattr(package, "blender_assets")


def record(key, name, **extra):
    return dict(
        key=key, name=name, nodes=[], outputs=dict(surface={}), settings=dict(displacement_method="BUMP"), **extra
    )


def manifest(materials):
    return dict(
        source="/assets/tree.blend",
        selector_type="COLLECTION",
        selector="tree",
        frame=1.0,
        dependency_versions={},
        materials=materials,
        meshes=[dict(key="mesh_0", arrays={})],
        array_identities={},
        instances=[
            dict(
                key="instance_0",
                mesh_key="mesh_0",
                transform=[1.0, 0, 0, 0, 0, 0, 1, 0, 0, -1, 0, 0, 0, 0, 0, 1],
                material_keys=[m["key"] for m in materials],
            )
        ],
        diagnostics=[],
    )


def fake_import(calls):
    from cycles_houdini.blender import blend_materials

    def import_materials(records, asset_root, parent=None, diagnostics=None):
        (item,) = records
        calls.append(item["key"])
        if item["name"] == "twig":
            raise blend_materials.BlendMaterialError("twig / Script: Unsupported connected Blender node type")
        if item["name"] == "leaves" and diagnostics is not None:
            diagnostics.append(
                dict(
                    code="blender.material.approximation",
                    severity="warning",
                    source="leaves",
                    message="leaves / Object Info: " + blend_materials.OBJECT_INFO_RANDOM_WARNING,
                )
            )
        parent.createNode("cycles::material::1", item["name"])
        return {item["key"]: "/mat/" + item["name"]}

    return import_materials


def test_untranslatable_material_renders_its_geometry_with_the_fallback(assets, monkeypatch):
    from cycles_houdini.blender import blend_materials

    calls = []
    monkeypatch.setattr(blend_materials, "import_materials", fake_import(calls))
    materials = [record("k_bark", "bark"), record("k_twig", "twig"), record("k_leaves", "leaves")]
    entry = assets.module._prepare_result(manifest(materials), {}, "working")
    (instance,) = entry["instances"]
    # The empty slot inherits the Houdini material binding or the ROP fallback.
    assert instance["material_paths"] == ("/mat/bark", "", "/mat/leaves")
    codes = {issue["code"]: issue for issue in entry["diagnostics"]}
    untranslated = codes["blend.material_untranslated"]
    assert untranslated["severity"] == "warning"
    assert untranslated["message"] == (
        "Blender material twig cannot be translated: twig / Script: Unsupported connected Blender node type"
    )
    assert "ROP fallback material" in untranslated["action"]
    assert "Object Info Random differs from Blender" in codes["blender.material.approximation"]["message"]
    # The failed builder network is not left behind, and the verdict is cached.
    assert [child.destroyed for child in assets.root.children] == [False, True, False]
    assert assets.module._prepare_result(manifest(materials), {}, "working")["instances"][0]["material_paths"] == (
        "/mat/bark",
        "",
        "/mat/leaves",
    )
    assert calls == ["k_bark", "k_twig", "k_leaves"]


def test_exporter_placeholder_and_unshareable_textures_fall_back_without_import(assets, monkeypatch, tmp_path):
    from cycles_houdini.blender import blend_materials

    calls = []
    monkeypatch.setattr(blend_materials, "import_materials", fake_import(calls))
    image = dict(filename="textures/leaf.png", dependencies=[dict(filename="textures/leaf.png", sha256="0")])
    missing = record("k_leaf", "leaf_textured")
    missing["nodes"] = [dict(id="n0", image=image)]
    placeholder = dict(record("k_broken", "broken"), unsupported="Image Texture has no image: Leaf", outputs={})
    source = tmp_path / "evaluation"
    source.mkdir()
    assets.module._share_textures(source, [missing, placeholder])
    assert missing["unsupported"].startswith("Texture unavailable: Invalid owned Blender texture")
    entry = assets.module._prepare_result(manifest([missing, placeholder]), {}, "working")
    assert entry["instances"][0]["material_paths"] == ("", "")
    assert calls == []
    messages = [issue["message"] for issue in entry["diagnostics"]]
    assert "Blender material broken cannot be translated: Image Texture has no image: Leaf" in messages
    assert any(
        message.startswith("Blender material leaf_textured cannot be translated: Texture") for message in messages
    )


def test_bounds_refresh_publishes_geometry_bounds_when_prototype_adoption_fails(monkeypatch):
    bounds = importlib.import_module("cycles_houdini.blender.blend_bounds")
    package = importlib.import_module("cycles_houdini.blender")
    worker = importlib.import_module("cycles_houdini.blender.blend_worker")
    node_bounds = importlib.import_module("cycles_houdini.blender.node_bounds")
    version = (1, 2, 3, 4)
    published = []

    def resolve(spec):
        reference, frame, native = spec
        source, selector = reference.split("#object=")
        return ("blender.exe", source, "OBJECT", selector, frame, None, "working", native), dict(selector=selector)

    def accept(key, raw, directory, launch, epoch):
        raise ValueError("Blender mesh has an invalid material slot")

    assets = SimpleNamespace(
        _resolve_spec=resolve,
        _hip_event=object(),
        accept_evaluation=accept,
        resident_evaluation=lambda key: None,
        _trim_prototypes=lambda: None,
    )
    monkeypatch.setattr(package, "blender_assets", assets, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "hou",
        SimpleNamespace(hipFile=SimpleNamespace(eventCallbacks=lambda: [], addEventCallback=lambda callback: None)),
    )
    monkeypatch.setattr(
        worker,
        "evaluate_many",
        lambda source, executable, requests, directory, **unused: [
            (dict(source=source, dependency_versions={source: version}), {}) for _ in requests
        ],
    )
    monkeypatch.setattr(worker, "_epoch", lambda source: 0)
    monkeypatch.setattr(worker, "launch_signature", lambda *unused: "launch")
    monkeypatch.setattr(worker, "_signature", lambda path: version)
    monkeypatch.setattr(
        bounds,
        "evaluated_record",
        lambda manifest, arrays, **unused: dict(dependency_versions=manifest["dependency_versions"], known=True),
    )
    monkeypatch.setattr(bounds, "publish_record", lambda reference, frame, entry, **unused: published.append(reference))
    monkeypatch.setattr(node_bounds, "drain_pending_node_bounds", lambda **unused: None)
    monkeypatch.setattr(bounds, "publication_generation", lambda: (0, 0))
    monkeypatch.setattr(bounds, "_refresh_generation", 0)
    monkeypatch.setattr(bounds, "_refresh_job", None)
    monkeypatch.setattr(bounds, "_refresh_running", None)
    spec = ("/assets/tree.blend#object=tree", 1.0, True)
    job = bounds.refresh_many([spec])
    assert job.done.wait(3)
    assert threading.current_thread() is threading.main_thread()
    try:
        assert job.publish()["known"] and published == [spec[0]]
        (key,) = job.resolved_keys
        assert "invalid material slot" in str(job.adoption_errors[key])
        # Render preparation joining this batch falls through to its own evaluation.
        assert job.accept(key) is None
    finally:
        job.close()


def test_reference_export_records_unsupported_materials_instead_of_failing(monkeypatch, tmp_path):
    from cycles_blender import blend_material_export

    working = SimpleNamespace(getName=lambda: "Linear Rec.709", getInteropID=lambda: "lin_rec709_scene")
    config = SimpleNamespace(getColorSpace=lambda name: working)
    monkeypatch.setitem(
        sys.modules,
        "PyOpenColorIO",
        SimpleNamespace(GetCurrentConfig=lambda: config, ROLE_SCENE_LINEAR="scene_linear"),
    )
    broken = SimpleNamespace(name_full="twig", library=None, node_tree=SimpleNamespace(nodes=[]))
    with pytest.raises(blend_material_export.BlendMaterialExportError, match="one active Cycles output"):
        blend_material_export.export_materials([broken], tmp_path)  # library/browser contract stays strict
    result = blend_material_export.export_materials([broken], tmp_path, tolerant=True)
    (placeholder,) = result["materials"]
    assert placeholder["key"] == blend_material_export.material_key(broken)
    assert placeholder["unsupported"] == "Material needs one active Cycles output: twig"
    assert placeholder["nodes"] == [] and placeholder["outputs"] == {}
