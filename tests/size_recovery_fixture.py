# SPDX-License-Identifier: Apache-2.0
"""Native grid/line geometry with deliberately malformed optional size data."""

import hou


def size_fixture(curves=False):
    obj = hou.node("/obj").createNode("geo", "curve_sizes" if curves else "point_sizes", run_init_scripts=False)
    if curves:
        base = obj.createNode("line")
        base.setParms(dict(points=6, dist=2.0))
        base.parmTuple("origin").set((0, -1, 0))
        base.parmTuple("dir").set((0, 1, 0))
    else:
        base = obj.createNode("grid")
        base.setParms(dict(rows=2, cols=3, orient=0))
        base.parmTuple("size").set((2.0, 1.0))
    node = obj.createNode("python", "size_test")
    node.setInput(0, base)
    node.addSpareParmTuple(hou.IntParmTemplate("mode", "Size Test", 1))
    node.addSpareParmTuple(hou.IntParmTemplate("owner", "Width Owner", 1))
    node.addSpareParmTuple(hou.FloatParmTemplate("fallback", "Reference Size", 1, default_value=(0.3,)))
    node.addSpareParmTuple(hou.FloatParmTemplate("time", "Time", 1))
    node.parm("time").setExpression("$T", hou.exprLanguage.Hscript)
    node.parm("python").set(
        """import hou, math
n=hou.pwd();g=n.geometry();curves=CURVES
if not curves:g.deletePrims(g.prims(),keep_points=True)
mode=n.evalParm('mode');fallback=n.evalParm('fallback')
# 0 healthy, 1 mixed invalid, 2 sanitized reference, 3 string,
# 4 integer, 5 float tuple, 6 all fallback, 7 absent,
# 8 subframe invalid, 9 sanitized subframe reference.
if mode in (8,9):mode=(1 if mode==8 else 2) if abs(hou.time())>1e-8 else 0
if mode!=7:
    owner=(hou.attribType.Point,hou.attribType.Vertex,hou.attribType.Prim,hou.attribType.Global)[n.evalParm('owner') if curves else 0]
    attr=g.addAttrib(owner,'width' if curves else 'pscale','bad' if mode==3 else 1 if mode==4 else (1.,2.) if mode==5 else .25)
    elements=g.points() if owner==hou.attribType.Point else tuple(v for p in g.prims() for v in p.vertices()) if owner==hou.attribType.Vertex else g.prims() if owner==hou.attribType.Prim else (g,)
    if mode not in (3,4,5):
        for i,item in enumerate(elements):
            value=.25+.02*i
            if mode in (1,2):
                # Keep one positive and one zero entry; repair only bad entries.
                value=(float('nan'),float('inf'),-1.,value,0.,float('-inf'))[i%6] if mode==1 else (fallback,fallback,fallback,value,0.,fallback)[i%6]
            if mode==6:value=fallback
            if owner==hou.attribType.Global:g.setGlobalAttribValue(attr,value)
            else:item.setAttribValue(attr,value)
""".replace("CURVES", str(curves))
    )
    node.setRenderFlag(True)
    node.setDisplayFlag(True)
    return obj, node
