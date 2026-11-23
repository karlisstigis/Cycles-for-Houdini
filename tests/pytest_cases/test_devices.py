# SPDX-License-Identifier: Apache-2.0
import pytest

from support.devices import resolve_device_argument

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

DEVICES = [
    {"backend": "CPU", "id": "CPU"},
    {"backend": "OPTIX", "id": "real_gpu_b"},
    {"backend": "OPTIX", "id": "real_gpu_a"},
]


def test_backend_cli_resolves_to_available_native_id():
    arguments, selected = resolve_device_argument("core_owned_image_smoke.py", ["--device", "OPTIX"], DEVICES)
    assert arguments == ["--device", "real_gpu_a"]
    assert selected == dict(backend="OPTIX", id="real_gpu_a")
    arguments, selected = resolve_device_argument("scene.py", ["--device", "OPTIX"], DEVICES, "real_gpu_b")
    assert arguments[-1] == "real_gpu_b"


def test_backend_enum_and_explicit_id_remain_script_owned():
    for script, value in [("houdini_blend_instances_smoke.py", "OPTIX"), ("scene.py", "real_gpu_b")]:
        assert resolve_device_argument(script, ["--device", value], DEVICES) == (["--device", value], None)


def test_missing_or_wrong_backend_is_a_failure():
    with pytest.raises(RuntimeError, match="unavailable"):
        resolve_device_argument("scene.py", ["--device", "OPTIX"], [])
    with pytest.raises(RuntimeError, match="does not belong"):
        resolve_device_argument("scene.py", ["--device", "OPTIX"], DEVICES, "CPU")


def test_blender_reference_ids_map_by_verified_name_and_pci():
    from support.devices import device_pci_bus_id, match_reference_optix_device

    host_id = "CUDA_NVIDIA GeForce RTX 4090_0000:03:00_OptiX"
    devices = [
        dict(id="OPTIX_NVIDIA GeForce RTX 4090_0000:01:00", name="NVIDIA GeForce RTX 4090", backend="OPTIX"),
        dict(id="OPTIX_NVIDIA GeForce RTX 4090_0000:03:00.0", name="NVIDIA GeForce RTX 4090", backend="OPTIX"),
        dict(id=host_id, name="NVIDIA GeForce RTX 4090", backend="CUDA"),
    ]
    assert device_pci_bus_id(host_id) == "0000:03:00"
    chosen = match_reference_optix_device(devices, name="NVIDIA GeForce RTX 4090", pci=device_pci_bus_id(host_id))
    assert chosen is devices[1]
    with pytest.raises(RuntimeError, match="exactly one"):
        match_reference_optix_device(devices, name="NVIDIA GeForce RTX 4090")
    with pytest.raises(RuntimeError, match="PCI identity differs"):
        match_reference_optix_device(devices, name="NVIDIA GeForce RTX 4090", pci="0000:05:00")


def test_blender_reference_without_pci_requires_unique_optix_name():
    from support.devices import device_pci_bus_id, match_reference_optix_device

    device = dict(id="OPTIX_uuid", name="NVIDIA GeForce RTX 3080 Ti", backend="OPTIX")
    assert device_pci_bus_id(device["id"]) is None
    assert match_reference_optix_device([device], name=device["name"].lower(), pci="0000:01:00") is device
    for devices in ([], [dict(device, backend="CUDA")], [device, dict(device, id="OPTIX_other_uuid")]):
        with pytest.raises(RuntimeError, match="exactly one"):
            match_reference_optix_device(devices, name=device["name"], pci="0000:01:00")
