# SPDX-License-Identifier: Apache-2.0
"""Native geometry with deliberately malformed optional metadata for recovery tests."""

import hou


def identity_fixture(kind):
    obj = hou.node("/obj").createNode("geo", "identity_" + kind, run_init_scripts=False)
    if kind in ("mesh", "subdivision"):
        base = obj.createNode("box")
        base.parmTuple("size").set((1.4, 1.4, 1.4))
    elif kind == "curves":
        line = obj.createNode("line")
        line.parm("points").set(4)
        line.parmTuple("origin").set((-0.8, -0.6, 0))
        line.parm("dist").set(1.2)
        other = obj.createNode("xform")
        other.setInput(0, line)
        other.parm("tx").set(1.6)
        base = obj.createNode("merge")
        base.setInput(0, line)
        base.setInput(1, other)
    else:
        grid = obj.createNode("grid")
        grid.setParms(dict(rows=2, cols=2, orient=0))
        grid.parmTuple("size").set((2.5, 1.2))
        base = obj.createNode("attribwrangle", "loose_points")
        base.setInput(0, grid)
        base.parm("class").set(0)
        base.parm("snippet").set("for(int p=nprimitives(0)-1;p>=0;--p) removeprim(0,p,0);")
        if kind == "packed":
            box = obj.createNode("box", "prototype")
            box.parmTuple("size").set((0.5, 0.5, 0.5))
            copy = obj.createNode("copytopoints")
            copy.setInput(0, box)
            copy.setInput(1, base)
            copy.parm("pack").set(1)
            base = copy
    mover = obj.createNode("xform", "animation")
    mover.setInput(0, base)
    mover.parm("tx").setExpression("$T*12", hou.exprLanguage.Hscript)
    source = obj.createNode("python", "optional_metadata")
    source.setInput(0, mover)
    source.addSpareParmTuple(hou.IntParmTemplate("mode", "Metadata Test", 1))
    source.parm("python").set("KIND=" + repr(kind) + "\n" + METADATA)
    source.setRenderFlag(True)
    source.setDisplayFlag(True)
    return obj, source


METADATA = """import hou
n=hou.pwd();g=n.geometry();mode=n.evalParm('mode')
g.addAttrib(hou.attribType.Point,'pscale',.22)
g.addAttrib(hou.attribType.Point,'width',.18)
g.addAttrib(hou.attribType.Point,'v',(12.,0.,0.))
# These are attribute-only corruptions; all topology/positions come from native SOPs.
if KIND=='subdivision':
    names=('osd_scheme','osd_vtxboundaryinterpolation','osd_fvarlinearinterpolation','osd_creasingmethod','osd_trianglesubdiv')
    if 1<=mode<=5:g.addAttrib(hou.attribType.Global,names[mode-1],'unsupported override')
    if mode in (6,7):g.createPrimGroup('subdivision_hole').add(g.prims()[0])
    if mode==7:
        for name in names:g.addAttrib(hou.attribType.Global,name,(99,99))
else:
    for owner,elements in ((hou.attribType.Point,g.points()),(hou.attribType.Prim,g.prims())):
        if mode==0:continue
        if mode==1:
            g.addAttrib(owner,'id',7);g.addAttrib(owner,'name','merged_sim')
        elif mode==2:
            g.addAttrib(owner,'id',(1.,2.,3.));g.addAttrib(owner,'name',99.)
        elif mode==3:
            attr=g.addAttrib(owner,'id',0);g.addAttrib(owner,'name','merged_sim')
            for index,item in enumerate(elements):item.setAttribValue(attr,index+10)
        elif mode==4:
            attr=g.addAttrib(owner,'name','');g.addAttrib(owner,'id','unused string')
            for index,item in enumerate(elements):item.setAttribValue(attr,'element_'+str(index))
        elif mode==5:
            attr=g.addAttrib(owner,'id',0);g.addAttrib(owner,'name','')
            for index,item in enumerate(elements):item.setAttribValue(attr,7 if index<2 else index+10)
        elif mode==6:
            g.addAttrib(owner,'id',(1,2));g.addAttrib(owner,'name',('left','right'))
"""
