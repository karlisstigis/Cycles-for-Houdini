# SPDX-License-Identifier: Apache-2.0
"""Owned large-scene fixture and resource-scaled retention bounds for the soak."""

import statistics

from support.devices import device_pci_bus_id

MIB = 2**20


def retention_bounds(sample, image_bytes, frame_bytes):
    """Allow a bounded extra scene staging copy, plus raster/display queues.

    These are growth allowances after warm-up, never limits on scene size or
    peak process commit. Exact scene resource counts are checked separately.
    DXGI includes driver allocations, so allow one eighth of the retained
    device ledger as well as two image payloads/eight display frames.
    """
    geometry = sample["counters"]["owned_geometry_bytes"]
    scene = geometry + image_bytes
    return dict(
        host=max(2 * scene, 4 * image_bytes, 16 * frame_bytes),
        worker=max(scene, 2 * image_bytes, 8 * frame_bytes),
        device=max(65536, frame_bytes),
        gpu=max(sample["counters"]["device_allocation_bytes"] // 8, 2 * image_bytes, 8 * frame_bytes),
    )


def gpu_usage(memory):
    return {
        adapter["luid"]: {
            "name": adapter["name"],
            "local": adapter["local"]["current_usage_bytes"],
            "nonlocal": adapter["nonlocal"]["current_usage_bytes"],
        }
        for adapter in memory.get("gpu", {}).get("adapters", [])
    }


def retention_window(samples, values):
    """Disjoint equally sized early and late windows from one comparable phase."""
    assert len(samples) == len(values) >= 2
    count = min(4, len(values) // 2)
    early, late = values[:count], values[-count:]
    return dict(
        observations=len(values),
        observations_per_window=count,
        early_labels=[sample["label"] for sample in samples[:count]],
        late_labels=[sample["label"] for sample in samples[-count:]],
        early_seconds=[samples[0]["time"], samples[count - 1]["time"]],
        late_seconds=[samples[-count]["time"], samples[-1]["time"]],
        early_range_bytes=[min(early), max(early)],
        late_range_bytes=[min(late), max(late)],
        median_growth_bytes=statistics.median(late) - statistics.median(early),
        peak_to_low_bytes=max(values) - min(values),
    )


def selected_adapter(memory, board, *, require_usage=False):
    """Require an exact current-boot LUID and both measured segments."""
    gpu = memory["gpu"]
    assert "enumeration_error_hresult" not in gpu, gpu
    matches = [adapter for adapter in gpu.get("adapters", []) if adapter.get("luid") == board["luid"]]
    assert len(matches) == 1, (board, gpu)
    adapter = matches[0]
    assert adapter["node"] == board["node"] and adapter["vendor_id"] == 0x10DE, (board, adapter)
    segments = {name: adapter.get(name, {}).get("current_usage_bytes") for name in ("local", "nonlocal")}
    assert all(type(value) is int and value >= 0 for value in segments.values()), (board, adapter)
    assert not require_usage or sum(segments.values()) > 0, (board, adapter)
    return segments


def cuda_board(device_id):
    """Map the Cycles PCI location to the current CUDA UUID and DXGI LUID without a context."""
    import ctypes as ct

    requested = device_pci_bus_id(device_id)
    assert device_id.startswith("CUDA_") and requested, f"Expected a single NVIDIA device ID, received {device_id}"
    driver = ct.WinDLL("nvcuda.dll")

    def call(name, signature, *args):
        fn = getattr(driver, name)
        fn.argtypes = signature
        fn.restype = ct.c_int
        result = fn(*args)
        if result:
            raise RuntimeError(f"{name} failed with CUDA status {result}")

    call("cuInit", [ct.c_uint], 0)
    count = ct.c_int()
    call("cuDeviceGetCount", [ct.POINTER(ct.c_int)], ct.byref(count))
    matches = []
    for index in range(count.value):
        handle = ct.c_int()
        call("cuDeviceGet", [ct.POINTER(ct.c_int), ct.c_int], ct.byref(handle), index)
        pci = ct.create_string_buffer(32)
        call("cuDeviceGetPCIBusId", [ct.c_void_p, ct.c_int, ct.c_int], pci, len(pci), handle)
        address = pci.value.decode("ascii").lower()
        if address.rsplit(".", 1)[0] == requested:
            matches.append((handle, address))
    assert len(matches) == 1, (requested, matches)
    handle, address = matches[0]
    luid = (ct.c_uint32 * 2)()
    mask = ct.c_uint()
    uuid = (ct.c_ubyte * 16)()
    call("cuDeviceGetLuid", [ct.c_void_p, ct.POINTER(ct.c_uint), ct.c_int], luid, ct.byref(mask), handle)
    call("cuDeviceGetUuid", [ct.c_void_p, ct.c_int], uuid, handle)
    assert mask.value == 1, f"Own-process DXGI telemetry observes only node 0, CUDA mask was {mask.value:#x}"
    return dict(
        device_id=device_id, pci_bus_id=address, uuid=bytes(uuid).hex(), luid=f"{luid[1]:08x}:{luid[0]:08x}", node=0
    )


def create_large_scene(texture, device, resolution=(480, 270)):
    import hou
    import cycles_houdini as ch
    from cycles_houdini import device_selection

    material = ch.create_material(name="soak_material", shader="emission")
    material.node("surface_shader").parm("strength").set(1)
    image = material.createNode("cycles::image_texture::1", "soak_image")
    image.setParms(dict(filename=str(texture), colorspace="data", alpha_type="ignore"))
    material.node("surface_shader").setNamedInput("color", image, "color")
    obj = hou.node("/obj").createNode("geo", "soak_subject", run_init_scripts=False)
    dense = obj.createNode("grid", "dense_receiver")
    dense.setParms(dict(type=0, orient=0, rows=513, cols=513))
    dense.parmTuple("size").set((12, 12))
    reverse = obj.createNode("reverse")
    reverse.setInput(0, dense)
    deform = obj.createNode("xform", "dense_edit")
    deform.setInput(0, reverse)
    uv = obj.createNode("attribwrangle")
    uv.setInput(0, deform)
    uv.parm("snippet").set("v@uv=set(.5,.5,0);")
    uv.setDisplayFlag(True)
    uv.setRenderFlag(True)
    obj.parm("shop_materialpath").set(material.path())

    instances = hou.node("/obj").createNode("geo", "soak_instances", run_init_scripts=False)
    prototype = instances.createNode("grid", "shared_prototype")
    prototype.setParms(dict(type=0, orient=0, rows=33, cols=33))
    prototype.parmTuple("size").set((0.10, 0.10))
    proto_uv = instances.createNode("attribwrangle")
    proto_uv.setInput(0, prototype)
    proto_uv.parm("snippet").set("v@uv=set(.5,.5,0);")
    points = instances.createNode("grid", "instance_points")
    points.setParms(dict(type=0, orient=0, rows=64, cols=64))
    points.parmTuple("size").set((10, 6))
    move = instances.createNode("xform", "instance_edit")
    move.setInput(0, points)
    move.parm("tz").set(0.2)
    copies = instances.createNode("copytopoints")
    copies.setInput(0, proto_uv)
    copies.setInput(1, move)
    copies.parm("pack").set(1)
    copies.setDisplayFlag(True)
    copies.setRenderFlag(True)
    instances.parm("shop_materialpath").set(material.path())

    cops = hou.node("/img").createNode("copnet", "soak_cops")
    cops.setParms(dict(setres=1, res1=512, res2=512, setprecision=1, precision=1))
    color = cops.createNode("constant", "color")
    color.parm("signature").set("f3")
    color.parmTuple("f3").set((0.3, 0.2, 0.7))
    camera = hou.node("/obj").createNode("cam", "soak_camera")
    camera.setParms(dict(tz=5, projection=1, orthowidth=10))
    light = hou.node("/obj").createNode("hlight", "soak_light")
    light.setParms(dict(tx=2, ty=3, tz=4, light_intensity=3))
    rop = hou.node("/out").createNode("cycles_ipr", "soak_render")
    rop.setParms(
        dict(
            vobject="soak_subject soak_instances soak_light",
            camera=camera.path(),
            resx=resolution[0],
            resy=resolution[1],
            resolution_percent=100,
            ipr_resolution_percent=100,
            ipr_samples=1048576,
            ipr_adaptive_sampling=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            use_texture_cache=0,
        )
    )
    device_selection.set_devices(rop, device)
    return dict(
        obj=obj,
        geometry=deform,
        camera=camera,
        light=light,
        rop=rop,
        image=image,
        material=material,
        instances=move,
        cop=color,
        inventory=dict(
            unique_triangles=2 * 512 * 512 + 2 * 32 * 32,
            packed_instances=4096,
            effective_triangles=2 * 512 * 512 + 4096 * 2 * 32 * 32,
            file_raster=[1024, 1024, 3],
            cop_raster=[512, 512, 3],
            display=[*resolution, 4],
        ),
    )
