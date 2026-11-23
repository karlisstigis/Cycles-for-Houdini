# SPDX-License-Identifier: Apache-2.0
from support.paths import artifacts_root
from native_test_runtime import runtime_executable
from cycles_houdini.device_selection import set_devices as set_rop_devices
import os
from pathlib import Path
import subprocess
import hou
import cycles_houdini as ch

MISSING_DENSITY = {2, 3, 10}
IGNORED_FIELD = {1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15}
RECOVERY_MODES = [0] + [m for broken in range(1, 17) for m in (broken, 0)]


def fixture(device):
    root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
    dest = artifacts_root() / "examples/volume-field-recovery"
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(runtime_executable("ch_harness.exe")), "--write-volume-motion-fixtures", str(dest)], check=True)
    obj = hou.node("/obj").createNode("geo", "volume_recovery", run_init_scripts=False)
    file = obj.createNode("file")
    file.parm("file").set(str(dest / "vector.vdb"))
    stash = obj.createNode("stash")
    stash.parm("stash").set(file.geometry())
    file.destroy()
    extra = []
    for filename in ("integer.vdb", "empty.vdb"):
        source = obj.createNode("file")
        source.parm("file").set(str(dest / filename))
        kept = obj.createNode("stash")
        kept.parm("stash").set(source.geometry())
        source.destroy()
        extra.append(kept)
    merge = obj.createNode("merge")
    merge.setInput(0, stash)
    merge.setInput(1, stash)
    for i, node in enumerate(extra, 2):
        merge.setInput(i, node)
    edit = obj.createNode("python", "field_test")
    edit.setInput(0, merge)
    edit.addSpareParmTuple(
        hou.IntParmTemplate(
            "mode",
            "Field Test",
            1,
            help="0 valid; 1 duplicate; 2 missing density; 3 vector density; 4 unnamed; 5 vector temperature; 6 scalar color; 7 scalar velocity; 8 reserved UV; 9 integer extra; 10 integer density; 11 invalid then valid density; 12 empty density; 13 vector heat; 14 vector flame; 15 vector velocity component; 16 custom vector.",
        )
    )
    edit.parm("python").set("""import hou
g=hou.pwd().geometry();mode=hou.pwd().evalParm('mode')
d=[p for p in g.prims() if p.attribValue('name')=='density']
v=[p for p in g.prims() if p.attribValue('name') in ('vel','velocity')]
i=[p for p in g.prims() if p.attribValue('name')=='integer_field']
keep=d[:1]
if mode==1:keep=d[:2]
elif mode==2:keep=v[:1]
elif mode==3:keep=v[:1];keep[0].setAttribValue('name','density')
elif mode in (4,5,8,13,14,15,16):
    keep=d[:1]+v[:1];v[0].setAttribValue('name',{4:'',5:'temperature',8:'uv',13:'heat',14:'flame',15:'velocity_x',16:'arbitrary_custom_vector'}[mode])
elif mode in (6,7):
    keep=d[:2];d[1].setAttribValue('name','color' if mode==6 else 'vel')
elif mode==9:keep=d[:1]+i
elif mode==10:keep=i;keep[0].setAttribValue('name','density')
elif mode==11:
    # First vector precedes the second valid density in SOP primitive order.
    keep=v[:1]+d[1:2];v[0].setAttribValue('name','density')
elif mode==12:keep=d[2:3]
g.deletePrims([p for p in g.prims() if p not in keep],keep_points=False)
""")
    absorb = ch.create_material(name="volume_absorption", shader="absorption_volume")
    absorb.node("volume_shader").parmTuple("color").set((0, 0, 0))
    obj.parm("shop_materialpath").set(absorb.path())
    material = ch.create_material(name="visible_mesh", shader="emission")
    material.node("surface_shader").parmTuple("color").set((0.1, 0.7, 0.2))
    material.node("surface_shader").parm("strength").set(1)
    box = obj.createNode("box", "unaffected_mesh")
    box.parmTuple("size").set((0.3, 0.5, 0.2))
    box.parm("tx").set(1.5)
    bind = obj.createNode("attribwrangle", "mesh_material")
    bind.setInput(0, box)
    bind.parm("class").set(1)
    bind.parm("snippet").set('s@shop_materialpath="%s";' % material.path())
    output = obj.createNode("merge", "OUT")
    output.setInput(0, edit)
    output.setInput(1, bind)
    output.setDisplayFlag(True)
    output.setRenderFlag(True)
    cam = hou.node("/obj").createNode("cam", "camera")
    cam.setParms(dict(tz=5, projection=1, orthowidth=4))
    rop = hou.node("/out").createNode("cycles_ipr", "volume_recovery")
    set_rop_devices(rop, device)
    rop.setParms(
        dict(
            source_mode="objects",
            vobject=obj.path(),
            camera=cam.path(),
            resx=80,
            resy=48,
            samples=256,
            ipr_samples=256,
            use_denoise=0,
            ipr_use_denoise=0,
            allowmotionblur=0,
            use_material_assignments=1,
            material_kind="volume_absorption",
        )
    )
    rop.parmTuple("basecolor").set((0, 0, 0))
    rop.parmTuple("background").set((1, 1, 1))
    obj.layoutChildren()
    return rop, edit, dest


def check(frame, status, mode):
    import numpy as np

    pixels = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(48, 80, 4)
    value = pixels[21:27, 37:43, :3].mean()
    assert pixels[24, 70, 1] > 0.6 and pixels[24, 70, 0] < 0.2, pixels[24, 70].tolist()
    assert np.isfinite(pixels).all() and status["state"] != "failed"
    if mode in MISSING_DENSITY or mode == 12:
        assert abs(value - 1) < 0.001, (mode, value)
    else:
        assert 0.1 < value < 0.9, (mode, value)
    codes = [d["code"] for d in status["source_diagnostics"]]
    assert ("volume.density_unavailable" in codes) == (mode in MISSING_DENSITY), codes
    assert ("volume.field_ignored" in codes) == (mode in IGNORED_FIELD), codes
    if mode not in MISSING_DENSITY | IGNORED_FIELD:
        assert not codes, codes
    return pixels.copy()
