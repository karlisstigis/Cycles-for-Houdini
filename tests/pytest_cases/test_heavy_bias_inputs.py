# SPDX-License-Identifier: Apache-2.0
"""Bias-suite inputs: like-for-like stock/fork CLI backends and honoured light placements."""

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import xml.etree.ElementTree as ET

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from cycles_cli_backend import describe, pe_imports, require_like_backends  # noqa: E402

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def bias_script():
    spec = importlib.util.spec_from_file_location("run_heavy_bias", SCRIPTS / "run-heavy-bias.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_pe(path, imports, delay_imports=()):
    """Minimal PE32+ with one section holding the import and delay-import tables."""
    section_rva, section_raw, section_size = 0x1000, 0x200, 0x400
    body = bytearray(section_size)
    names_at = 0x200
    descriptors, delayed = [], []
    for name in (*imports, *delay_imports):
        encoded = name.encode("ascii") + b"\0"
        body[names_at : names_at + len(encoded)] = encoded
        (descriptors if name in imports else delayed).append(section_rva + names_at)
        names_at += len(encoded)
    for index, rva in enumerate(descriptors):
        struct.pack_into("<5I", body, 20 * index, 0, 0, 0, rva, 0)
    delay_offset = 0x100
    for index, rva in enumerate(delayed):
        struct.pack_into("<8I", body, delay_offset + 32 * index, 1, rva, 0, 0, 0, 0, 0, 0)
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, 0x20B)
    struct.pack_into("<II", optional, 112 + 8, section_rva, 20 * (len(descriptors) + 1))
    if delayed:
        struct.pack_into("<II", optional, 112 + 8 * 13, section_rva + delay_offset, 32 * (len(delayed) + 1))
    header = bytearray(0x40)
    header[:2] = b"MZ"
    struct.pack_into("<I", header, 0x3C, 0x40)
    coff = struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, len(optional), 0x22)
    section = b".idata\0\0" + struct.pack("<IIII", section_size, section_rva, section_size, section_raw) + bytes(16)
    image = header + b"PE\0\0" + coff + optional + section
    image += bytes(section_raw - len(image)) + body
    path.write_bytes(image)
    return path


def test_pe_imports_reads_import_and_delay_tables(tmp_path):
    exe = fake_pe(tmp_path / "cycles.exe", ("OpenImageIO.dll", "embree4.dll"), ("tbb12.dll",))
    assert pe_imports(exe) == ["openimageio.dll", "embree4.dll", "tbb12.dll"]


def test_describe_identifies_backend_and_resolves_embree_on_the_search_path(tmp_path):
    runtime = tmp_path / "embree" / "bin"
    runtime.mkdir(parents=True)
    (runtime / "embree4.dll").write_bytes(b"embree runtime")
    embree = describe(fake_pe(tmp_path / "fork.exe", ("embree4.dll", "tbb12.dll")), str(runtime))
    assert embree["backend"] == "embree"
    assert Path(embree["embree_dll_path"]) == (runtime / "embree4.dll").resolve()
    assert embree["embree_dll_sha256"] == hashlib.sha256(b"embree runtime").hexdigest()
    bvh2 = describe(fake_pe(tmp_path / "stock.exe", ("tbb12.dll",)), str(runtime))
    assert bvh2["backend"] == "bvh2" and bvh2["embree_dll_sha256"] is None
    with pytest.raises(FileNotFoundError, match="embree4.dll"):
        describe(tmp_path / "fork.exe", str(tmp_path / "nowhere"))


def test_unlike_backends_are_refused():
    embree = dict(backend="embree", embree_dll="embree4.dll", embree_dll_sha256="a" * 64)
    require_like_backends(embree, dict(embree, executable="other.exe"))
    with pytest.raises(RuntimeError, match="stock uses bvh2, fork uses embree"):
        require_like_backends(dict(backend="bvh2", embree_dll=None, embree_dll_sha256=None), embree)
    with pytest.raises(RuntimeError, match="different Embree runtimes"):
        require_like_backends(dict(embree, embree_dll_sha256="b" * 64), embree)
    with pytest.raises(RuntimeError, match="no ray-tracing backend record"):
        require_like_backends(None, embree)


def test_fork_capture_refuses_a_stock_capture_with_another_backend(tmp_path, monkeypatch):
    script = bias_script()
    (tmp_path / "stock").mkdir()
    stock = dict(backend="bvh2", embree_dll=None, embree_dll_sha256=None)
    (tmp_path / "stock" / "diffuse_cube-701.json").write_text(json.dumps(dict(bvh_backend=stock)), encoding="utf-8")
    integrator = tmp_path / "third_party/cycles/src/scene/integrator.cpp"
    integrator.parent.mkdir(parents=True)
    integrator.write_text("defaults", encoding="utf-8")
    manifest = dict(fork_integrator_sha256=hashlib.sha256(b"defaults").hexdigest(), includes={}, inputs={})
    (tmp_path / "inputs.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        script,
        "describe_backend",
        lambda executable, search_path: dict(backend="embree", embree_dll="embree4.dll", embree_dll_sha256="a" * 64),
    )
    fork = tmp_path / "fork.exe"
    fork.write_bytes(b"fork")
    with pytest.raises(RuntimeError, match="unlike Cycles builds"):
        script.run(tmp_path, fork, "fork", tmp_path, smoke=True)
    assert not any((tmp_path / "fork").iterdir())


CAUSTICS_LIGHT = """<cycles>
<integrator caustics_reflective="true" />
<state shader="PointLight_shader">
<light name="PointLight" light_type="point" strength="1 1 1" tfm="1 0 0 4  0 1 0 0  0 0 1 4" use_caustics="true" />
</state>
</cycles>"""


def test_light_tfm_becomes_an_enclosing_transform():
    root = ET.fromstring(CAUSTICS_LIGHT)
    assert bias_script().place_lights(root) == [dict(light="PointLight", translate="4 0 4")]
    state = root.find("state")
    assert state.get("shader") == "PointLight_shader"
    transform = state.find("transform")
    assert transform.get("translate") == "4 0 4"
    light = transform.find("light")
    assert light.get("tfm") is None and light.get("use_caustics") == "true"
    assert not [element for element in root.iter("light") if "tfm" in element.attrib]
    assert bias_script().place_lights(root) == []


def test_light_tfm_with_rotation_is_rejected():
    root = ET.fromstring(CAUSTICS_LIGHT.replace("1 0 0 4  0 1 0 0  0 0 1 4", "0 -1 0 4  1 0 0 0  0 0 1 4"))
    with pytest.raises(ValueError, match="translation-only"):
        bias_script().place_lights(root)
