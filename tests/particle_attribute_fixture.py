# SPDX-License-Identifier: Apache-2.0
"""Native SOP fixture: metadata edits preserve the actual geometry data IDs."""

import hou


def particle_attribute_fixture():
    obj = hou.node("/obj").createNode("geo", "particle_attributes", run_init_scripts=False)
    grid = obj.createNode("grid")
    grid.setParms(dict(rows=2, cols=3, orient=0))
    grid.parmTuple("size").set((3.0, 1.2))
    points = obj.createNode("attribwrangle", "points_only")
    points.setInput(0, grid)
    points.parm("snippet").set("for (int i=nprimitives(0)-1;i>=0;i--) removeprim(0,i,0);")
    parm = points.parm("class")
    labels = parm.parmTemplate().menuLabels()
    items = parm.parmTemplate().menuItems()
    parm.set(items[next(i for i, label in enumerate(labels) if label.startswith("Detail"))])
    node = obj.createNode("attribwrangle", "attribute_test")
    node.addSpareParmTuple(hou.IntParmTemplate("mode", "Attribute Test", 1))
    node.addSpareParmTuple(hou.FloatParmTemplate("speed", "Velocity", 1, default_value=(12.0,)))
    node.addSpareParmTuple(hou.ToggleParmTemplate("remove_first", "Remove First Point"))
    base = obj.createNode("attribwrangle", "render_attributes")
    base.setInput(0, points)
    base.parm("snippet").set('f@pscale=.3;v@v=set(chf("../attribute_test/speed"),0,0);i@id=@ptnum;')
    switch = obj.createNode("switch", "metadata")
    switch.setInput(0, base)
    snippets = [
        "f@age=1;f@life=2;i@dead=0;i@stopped=0;",
        "f@age=3;f@life=1;i@dead=1;i@stopped=1;",
        's@age="unused";v@life=set(1,2,3);f@dead=1;v@stopped=set(1,2,3);',
        "f@age=sqrt(-1.0);f@life=sqrt(-1.0);f@dead=sqrt(-1.0);f@stopped=sqrt(-1.0);",
        "f@age=-10;",
        "f@age=1e30;f@life=-1;",
    ]
    for i, code in enumerate(snippets, 1):
        attrs = obj.createNode("attribwrangle", "metadata_" + str(i))
        attrs.setInput(0, base)
        attrs.parm("snippet").set(code)
        switch.setInput(i, attrs)
    switch.parm("input").setExpression('ch("../attribute_test/mode")', hou.exprLanguage.Hscript)
    node.setInput(0, switch)
    node.parm("snippet").set('if(chi("remove_first") && @ptnum==0) removepoint(0,@ptnum);')
    node.setDisplayFlag(True)
    node.setRenderFlag(True)
    assert len(node.geometry().prims()) == 0 and len(node.geometry().points()) == 6, node.errors()
    return obj, node
