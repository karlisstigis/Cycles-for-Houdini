# SPDX-License-Identifier: Apache-2.0
"""Native source fixtures shared by headless and single-viewer recovery tests."""

import hou


def crease_fixture(name="crease_recovery"):
    obj = hou.node("/obj").createNode("geo", name, run_init_scripts=False)
    box = obj.createNode("box")
    box.parmTuple("size").set((1.4, 1.4, 1.4))
    source = obj.createNode("python", "weights")
    source.setInput(0, box)
    source.addSpareParmTuple(hou.IntParmTemplate("mode", "Weight Test", 1))
    source.parm("python").set("""import hou, math
n=hou.pwd();g=n.geometry();mode=n.evalParm('mode')
# 0 sharp; 1 invalid scalar; 2 smooth reference; 3 mixed invalid;
# 4 mixed sanitized reference; 5 malformed stream; 6 bad unused primitive;
# 7 valid vertex override of mode 6; 8 invalid corner; 9 valid corners.
edge=g.addAttrib(hou.attribType.Vertex,'creaseweight', 'bad' if mode==5 else 0.)
if mode==6:g.addAttrib(hou.attribType.Prim,'creaseweight','unused')
corner=g.addAttrib(hou.attribType.Point,'cornerweight',0.)
for p in g.prims():
    for v in p.vertices():
        if mode==5:continue
        i=v.number();w=10.
        if mode==1:w=float('nan')
        if mode in (2,8,9):w=0.
        if mode in (3,4):w=(10. if i%3==0 else (0. if mode==4 else (-1. if i%3==1 else float('inf'))))
        v.setAttribValue(edge,w)
for p in g.points():
    w=10. if mode in (8,9) and p.number()==0 else 0.
    if mode==8 and p.number()!=0:w=float('nan')
    p.setAttribValue(corner,w)
""")
    source.setRenderFlag(True)
    source.setDisplayFlag(True)
    return obj, source


def packed_fixture(name="packed_recovery"):
    obj = hou.node("/obj").createNode("geo", name, run_init_scripts=False)
    box = obj.createNode("box", "prototype")
    box.parmTuple("size").set((0.6, 0.6, 0.4))
    box.addSpareParmTuple(hou.FloatParmTemplate("depth", "Prototype Depth", 1, default_value=(0.4,)))
    box.parm("sizez").setExpression('ch("depth")', hou.exprLanguage.Hscript)
    grid = obj.createNode("grid", "targets")
    grid.setParms(dict(rows=2, cols=2, orient=0))
    grid.parmTuple("size").set((3.0, 1.5))
    targets = obj.createNode("attribwrangle", "population")
    targets.setInput(0, grid)
    targets.addSpareParmTuple(hou.IntParmTemplate("mode", "Population Test", 1))
    targets.addSpareParmTuple(hou.FloatParmTemplate("speed", "Packed Speed", 1, default_value=(24.0,)))
    targets.parm("snippet").set("""int mode=chi("mode");
i@id=(mode==1)?7:@ptnum;
if (mode==4 && @Time>0) i@id+=100;
@P.x+=@Time*chf("speed");
if ((mode==2 || mode==3) && @Time>0 && @ptnum==0) removepoint(0,@ptnum);
""")
    copies = obj.createNode("copytopoints", "instances")
    copies.setInput(0, box)
    copies.setInput(1, targets)
    copies.parm("pack").set(1)
    source = obj.createNode("python", "identity_test")
    source.setInput(0, copies)
    source.parm("python").set("""import hou
n=hou.pwd();g=n.geometry();mode=n.node('../population').evalParm('mode')
# Explicitly propagate IDs to primitives, independently of Copy attributes.
points=n.node('../population').geometry().points()
if mode!=3:
    attr=g.addAttrib(hou.attribType.Prim,'id',0)
    for prim,p in zip(g.prims(),points):prim.setAttribValue(attr,p.attribValue('id'))
else:
    for owner in (g.findPointAttrib,g.findPrimAttrib):
        attr=owner('id')
        if attr:attr.destroy()
t=hou.time()
if mode==5 and abs(t*24-round(t*24))>1e-6:raise hou.NodeError('Subframe unavailable')
""")
    # Explicit time dependency even when Copy to Points reuses a cached input.
    source.addSpareParmTuple(hou.FloatParmTemplate("time", "Time", 1))
    source.parm("time").setExpression("$T", hou.exprLanguage.Hscript)
    source.setRenderFlag(True)
    source.setDisplayFlag(True)
    return obj, source, targets, box
