# SPDX-License-Identifier: Apache-2.0
"""Stock-Houdini tapered fields; this module deliberately imports no renderer."""

import hou


SOURCE = """import hou, math
n=hou.pwd();g=n.geometry();mode=n.evalParm('mode');level=n.evalParm('level')
name=g.findPrimAttrib('name')
if g.findPrimAttrib('shop_materialpath') is None:g.addAttrib(hou.attribType.Prim,'shop_materialpath','')
tx=n.evalParm('taperx');ty=n.evalParm('tapery')
affine=hou.hmath.buildTransform(dict(rotate=(12,-18,n.evalParm('angle')),
    scale=(-1 if n.evalParm('mirror') else 1, .75, .5),shear=(.12,-.08,.05)))
for p in tuple(g.prims()):
    field=p.attribValue(name)
    if field=='density':
        nx,ny,nz=p.resolution()
        p.setAllVoxels(tuple(level*(1+.2*(x+.5)/nx+.1*(y+.5)/ny+.15*(z+.5)/nz)
            for z in range(nz) for y in range(ny) for x in range(nx)))
    p.setTransform(affine)
    p.setIntrinsicValue('taperx',tx);p.setIntrinsicValue('tapery',ty)
    p.setAttribValue('shop_materialpath',n.evalParm('integer_material') if mode in (1,10) else
        n.evalParm('emission_material') if n.evalParm('shading') else n.evalParm('float_material'))
    if mode==1:
        if field=='density':p.setAttribValue(name,'float_reference')
        elif field=='integer_auxiliary':p.setAttribValue(name,'density')
    if mode==2 and field=='density':p.setIntrinsicValue('taperx',float('nan'))
    if mode==3 and field=='density':p.setTransform(hou.hmath.buildScale((0,.75,.5)))
    if mode==4 and field=='density':p.setIntrinsicValue('taperx',1e-8)
    if mode==5 and field=='tint':p.setIntrinsicValue('taperx',1e-8)
    if mode==6 and field=='density':p.setVoxel((nx//2,ny//2,nz//2),float('nan'))
    if mode==7:
        if field=='density':p.setAttribValue(name,'float_reference')
        elif field=='tint':p.setAttribValue(name,'density')
    if mode==8 and field=='tint':p.setIntrinsicValue('taperx',float('nan'))
    if mode==9 and field=='tint':g.deletePrims((p,),keep_points=False)
    if mode==10:
        if field=='density':
            p.setIntrinsicValue('taperx',1e-8)
            p.setAttribValue('shop_materialpath',n.evalParm('float_material'))
        elif field=='integer_auxiliary':p.setAttribValue(name,'density')
"""


VECTOR_VALUES = """import hou
g=hou.pwd().geometry()
for p in g.prims():
    field=p.attribValue('name');axis=('tint.x','tint.y','tint.z').index(field)
    nx,ny,nz=p.resolution();base=(.18,.36,.62)[axis];gain=(.12,.08,.10)[axis]
    p.setAllVoxels(tuple(base+gain*((x+.5)/nx,(y+.5)/ny,(z+.5)/nz)[axis]
        for z in range(nz) for y in range(ny) for x in range(nx)))
"""


def build(obj, *, resolution=(13, 11, 29)):
    def volume(node_name, field, value, integer=False):
        node = obj.createNode("volume", node_name)
        node.setParms(
            dict(
                name=field,
                initialval1=value,
                uniformsamples=0,
                volborder=2,
                divsx=resolution[0],
                divsy=resolution[1],
                divsz=resolution[2],
            )
        )
        node.parmTuple("size").set((2, 1.5, 1))
        if integer:
            node.parm("type").set("int")
        return node

    density = volume("source_float", "density", 0.35)
    integer = volume("source_integer", "integer_auxiliary", 1, True)
    vectors = volume("source_vector_components", "tint", 0.18)
    vectors.setParms(dict(rank=1, components=1, initialval2=0.36, initialval3=0.62))
    varying = obj.createNode("python", "source_vector_values")
    varying.setInput(0, vectors)
    varying.parm("python").set(VECTOR_VALUES)
    joined = obj.createNode("volumevectorjoin", "source_native_vec3")
    joined.setInput(0, varying)
    joined.setParms(
        dict(
            setcomponents=1,
            components=3,
            keep=0,
            xgroup="@name=tint.x",
            ygroup="@name=tint.y",
            zgroup="@name=tint.z",
            wgroup="",
        )
    )
    merged = obj.createNode("merge", "all_native_fields")
    for i, node in enumerate((density, integer, joined)):
        merged.setInput(i, node)
    control = obj.createNode("python", "tapered_fields")
    control.setInput(0, merged)
    for template in (
        hou.IntParmTemplate("mode", "Test field state", 1),
        hou.FloatParmTemplate("taperx", "X taper", 1, default_value=(2,)),
        hou.FloatParmTemplate("tapery", "Y taper", 1, default_value=(1.5,)),
        hou.FloatParmTemplate("level", "Density", 1, default_value=(0.35,)),
        hou.FloatParmTemplate("angle", "Orientation", 1),
        hou.ToggleParmTemplate("mirror", "Reflected lattice"),
        hou.IntParmTemplate("shading", "Test emission", 1),
        hou.IntParmTemplate("border", "Test border rule", 1, default_value=(2,)),
        *(
            hou.StringParmTemplate(name, name, 1)
            for name in ("float_material", "integer_material", "emission_material")
        ),
    ):
        control.addSpareParmTuple(template)
    control.parm("python").set(SOURCE)
    control.setDisplayFlag(True)
    control.setRenderFlag(True)
    return control


def descriptors(control, obj=None):
    """Read SDK centres/maps before the renderer freezes or converts anything."""
    result = []
    for p in control.geometry().prims():
        full = hou.Matrix4(p.transform()) * hou.hmath.buildTranslate(p.vertex(0).point().position())
        if obj is not None:
            full *= obj.worldTransform()
        nx, ny, nz = p.resolution()
        item = dict(
            name=p.attribValue("name"),
            resolution=[nx, ny, nz],
            taper=[p.taperX(), p.taperY()],
            border=int(control.evalParm("border")),
            matrix=list(full.asTuple()),
            centres=[],
            material=p.attribValue("shop_materialpath"),
        )
        for index in ((0, 0, 0), (nx - 1, ny - 1, nz - 1), (nx // 2, ny // 2, nz // 2)):
            observed = p.indexToPos(index)
            if obj is not None:
                observed *= obj.worldTransform()
            item["centres"].append(dict(index=list(index), position=list(observed)))
        result.append(item)
    return result
