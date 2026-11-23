# SPDX-License-Identifier: Apache-2.0
"""Native SOP fixture: a broken packed disk alongside valid mixed geometry."""

from support.paths import artifacts_root
from pathlib import Path
import os
import hou


def assert_component_pixels(pixels, broken=False):
    """Independent screen locations for all six expected geometry components."""
    import numpy as np

    image = np.asarray(pixels).reshape(96, 192, 4)
    for x, y in ((0, 0), (-1, 0), (-2, 1), (0, 1), (-2, -0.5)):
        value = image[round((y / 3 + 0.5) * 96), round((x / 6 + 0.5) * 192)]
        assert value[1] > 0.6 and value[3] > 0.9, (x, y, value.tolist())
    value = image[48, 128]
    assert value[1] < 0.01 if broken else value[1] > 0.6, value.tolist()


def packed_source_fixture():
    root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
    assets = artifacts_root() / "examples/packed-source-recovery"
    assets.mkdir(parents=True, exist_ok=True)
    obj = hou.node("/obj").createNode("geo", "packed_source_recovery", run_init_scripts=False)
    box = obj.createNode("box", "prototype")
    box.parmTuple("size").set((0.6, 0.6, 0.4))
    path = assets / "prototype.bgeo.sc"
    box.geometry().saveToFile(str(path))
    disk = obj.createNode("file", "shared_disk")
    disk.setParms(dict(file=str(path), loadtype="delayed"))
    left = obj.createNode("xform", "valid_copy")
    left.setInput(0, disk)
    left.parm("tx").set(-1)
    right = obj.createNode("xform", "editable_copy")
    right.setInput(0, disk)
    right.parm("tx").set(1)
    edit = obj.createNode("attribwrangle", "source_failure")
    edit.setInput(0, right)
    edit.addSpareParmTuple(
        hou.IntParmTemplate(
            "mode",
            "Source Test",
            1,
            help="0: valid; 1: missing file; 2: absent-content reference; 3: unreadable file; 4: missing only at shutter samples.",
        )
    )
    bad = assets / "unreadable.bgeo.sc"
    bad.write_text("This is not geometry.", encoding="utf-8")
    missing = assets / "missing.bgeo.sc"
    edit.parm("class").set(1)
    edit.parm("snippet").set(
        """int mode=chi("mode");
if (mode==1 || (mode==4 && abs(@Time)>0.00001))
    setprimintrinsic(0,"unexpandedfilename",@primnum,"%s","set");
if (mode==3) setprimintrinsic(0,"unexpandedfilename",@primnum,"%s","set");
if (mode==2) removeprim(0,@primnum,1);
"""
        % (missing.as_posix(), bad.as_posix())
    )
    mesh = obj.createNode("xform", "ordinary_mesh")
    mesh.setInput(0, box)
    mesh.setParms(dict(tx=-2, ty=1))
    point = obj.createNode("add", "ordinary_point")
    point.parm("points").set(1)
    point.setParms(dict(usept0=1, pt0x=0, pt0y=1))
    assert len(point.geometry().points()) == 1
    curve = obj.createNode("line", "ordinary_curve")
    curve.setParms(dict(originx=-2, originy=-1, dist=1))
    merge = obj.createNode("merge", "mixed_geometry")
    for i, n in enumerate((disk, left, edit, mesh, point, curve)):
        merge.setInput(i, n)
    packed = obj.createNode("pack", "nested_container")
    packed.setInput(0, merge)
    output = obj.createNode("switch", "OUT")
    output.setInput(0, merge)
    output.setInput(1, packed)
    output.setDisplayFlag(True)
    output.setRenderFlag(True)
    obj.layoutChildren()
    return obj, output, edit
