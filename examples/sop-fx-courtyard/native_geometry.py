# SPDX-License-Identifier: Apache-2.0
"""Construct editable Houdini SOP networks; Python only authors the network.

All substantial cooking uses Houdini primitives, Copy to Points, normals and
compiled VEX. One tiny Python SOP is retained as an explicit compatibility test.
"""

import json
import hou


def geometry(obj, materials):
    branches = []

    def node(kind, name, source=None, **values):
        result = obj.createNode(kind, name)
        if source is not None:
            result.setInput(0, source)
        for key, value in values.items():
            if isinstance(value, tuple):
                result.parmTuple(key).set(value)
            else:
                result.parm(key).set(value)
        return result

    def wrangle(name, source, code, scope=2):
        return node("attribwrangle", name, source, **{"class": scope, "snippet": code})

    def parameter(sop, name, label, value, minimum, maximum):
        group = sop.parmTemplateGroup()
        template = (hou.IntParmTemplate if isinstance(value, int) else hou.FloatParmTemplate)(
            name, label, 1, default_value=(value,), min=minimum, max=maximum
        )
        group.append(template)
        sop.setParmTemplateGroup(group)

    def bind(source, material, name):
        result = wrangle(name, source, "s@shop_materialpath = " + json.dumps(materials[material].path()) + ";", 1)
        return result

    def surface(source, material, name):
        normals = node("normal", name + "_NORMALS", source)
        return bind(normals, material, name)

    def box(name, size, center, material):
        return surface(node("box", name + "_BOX", size=size, t=center), material, name)

    def torus(name, center, major, minor, material, orientation=1):
        return surface(
            node("torus", name + "_TORUS", t=center, radx=major, rady=minor, rows=12, cols=96, orient=orientation),
            material,
            name,
        )

    def sphere(name, center, radii, material, rows=24, columns=48):
        source = node("sphere", name + "_SPHERE", type=2, t=center, rad=radii, rows=rows, cols=columns)
        coords = wrangle(
            name + "_UV",
            source,
            f"vector p=normalize((@P-set{center})/set{radii}); v@uv=set(.5+atan2(p.z,p.x)/(2*M_PI),.5+asin(clamp(p.y,-1,1))/M_PI,0);",
        )
        return surface(coords, material, name)

    def merge(name, sources):
        result = node("merge", name)
        for i, source in enumerate(sources):
            result.setInput(i, source)
        return result

    def copy(name, prototype, targets, packed=False):
        result = node("copytopoints", name, prototype, pack=int(packed))
        result.setInput(1, targets)
        return result

    def points(name, count, code):
        base = node("line", name + "_POINTS", type=3, points=count)
        return wrangle(name, base, code)

    # Dense native Grid supplies UVs and enough vertices for true Cycles noise
    # displacement. Editing divisions no longer executes Python geometry loops.
    ground = node("grid", "NOISE_DISPLACED_GROUND", type=0, orient=2, size=(12, 10), t=(0, -0.18, 0))
    parameter(ground, "resolution", "Ground Divisions", 128, 16, 256)
    for name in ("rows", "cols"):
        ground.parm(name).setExpression('ch("resolution")+1', hou.exprLanguage.Hscript)
    uv = wrangle("GROUND_UV", ground, "v@uv=set((@P.x+6)/12,(@P.z+5)/10,0);")
    branches.append(surface(uv, "stone", "GROUND_MATERIAL"))

    # A subdivided native box, VEX deformation and native copies form the
    # sculpture. These are ordinary editable SOPs with stable artist controls.
    blade = node(
        "box",
        "BLADE_CONTROL_MESH",
        type=1,
        size=(0.62, 1, 0.10),
        t=(0.80, 0.5, 0),
        dodivs=1,
        divsx=2,
        divsy=81,
        divsz=2,
    )
    blade = node("convert", "BLADE_POLYGONS", blade)
    hero = wrangle(
        "TWISTED_TURBINE",
        blade,
        """
float t=@P.y; float a=t*chf("twist");
vector p=@P; p.x += .12*sin(t*M_PI);
@P=set(p.x*cos(a)-p.z*sin(a),.42+t*chf("height"),p.x*sin(a)+p.z*cos(a));
""",
    )
    parameter(hero, "height", "Sculpture Height", 3.2, 1.0, 5.0)
    parameter(hero, "twist", "Blade Twist (radians)", 2.6, 0.0, 6.28)
    parameter(hero, "blades", "Blade Count", 9, 3, 18)
    blade_targets = points("RADIAL_BLADE_TRANSFORMS", 9, "p@orient=quaternion(@ptnum*2*M_PI/@numpt, {0,1,0}); @P=0;")
    blade_targets.inputs()[0].parm("points").setExpression('ch("../TWISTED_TURBINE/blades")', hou.exprLanguage.Hscript)
    sculpture = surface(copy("NATIVE_TURBINE_BLADES", hero, blade_targets), "brass", "TURBINE_MATERIAL")
    rings = [
        torus("TURBINE_LOWER_RING", (0, 0.25, 0), 1.32, 0.09, "steel"),
        torus("TURBINE_COLLAR", (0, 0.40, 0), 1.12, 0.09, "steel"),
        torus("TURBINE_UPPER_RING", (0, 3.62, 0), 0.72, 0.09, "steel"),
        torus("TURBINE_LOWER_EMISSION", (0, 0.32, 0), 1.34, 0.034, "glow"),
        torus("TURBINE_UPPER_EMISSION", (0, 3.73, 0), 0.70, 0.034, "glow"),
    ]
    for ring, offset in ((rings[2], 0.42), (rings[4], 0.53)):
        ring.inputs()[0].inputs()[0].parm("ty").setExpression(
            f'ch("../TWISTED_TURBINE/height")+{offset}', hou.exprLanguage.Hscript
        )
    branches.append(
        merge("SCULPTURE_AND_RINGS", [sculpture, *rings, box("TURBINE_BASE", (2.8, 0.25, 2.8), (0, 0.1, 0), "steel")])
    )

    masonry = []
    for i, x in enumerate((-3.1, 3.1)):
        masonry.append(box(f"COLUMN_{i}", (0.8, 2.6, 0.8), (x, 1.1, -2.7), "steel"))
        for j, y in enumerate((0.25, 0.65, 1.9, 2.5)):
            masonry.append(box(f"COLUMN_{i}_BAND_{j}", (1.0, 0.16, 1.0), (x, y, -2.7), "ceramic"))
        masonry.append(torus(f"COLUMN_{i}_LIGHT", (x, 2.65, -2.7), 0.46, 0.06, "hot"))
    arch = torus("ARCH", (0, 2.25, -2.7), 3.25, 0.19, "ceramic", 2)
    masonry.append(wrangle("ARCH_UPPER_HALF", arch, "if (@P.y < 2.25) removeprim(0,@primnum,1);", 1))
    step = box("STEP", (0.72, 0.19, 0.6), (0, 0, 0), "ceramic")
    step_targets = points("STEP_POSITIONS", 6, "@P=set(-2.5+@ptnum,0,1.7);")
    masonry.append(copy("NATIVE_STEPS", step, step_targets))
    branches.append(merge("ARCH_AND_MASONRY", masonry))

    rock = sphere("ROCK_PROTOTYPE", (0, 0, 0), (1, 0.7, 0.8), "stone")
    rocks = points(
        "NOISE_DISPLACED_ROCKS",
        26,
        """
float a=rand(@ptnum+219.1)*2*M_PI; float r=fit01(rand(@ptnum+219.2),2,5);
@P=set(r*cos(a),fit01(rand(@ptnum+219.3),-.1,.15),r*.7*sin(a));
f@pscale=fit01(rand(@ptnum+219.4),.20,.55); i@id=@ptnum;
""",
    )
    parameter(rocks, "count", "Rock Count", 26, 1, 80)
    rocks.inputs()[0].parm("points").setExpression('ch("../NOISE_DISPLACED_ROCKS/count")', hou.exprLanguage.Hscript)
    branches.append(copy("NATIVE_ROCKS", rock, rocks))

    optical = []
    for i, (x, z, scale) in enumerate(((2.0, 0.85, 1.0), (-2.2, 0.4, 0.75), (1.9, -1.1, 0.8))):
        optical += [
            box(f"INSTRUMENT_{i}_BASE", (0.7, 0.42, 0.7), (x, 0.18, z), "brass"),
            sphere(
                f"INSTRUMENT_{i}_GLASS", (x, 0.75 * scale, z), (0.30 * scale, 0.44 * scale, 0.30 * scale), "glass", 30
            ),
            sphere(f"INSTRUMENT_{i}_FILAMENT", (x, 0.75 * scale, z), (0.035, 0.20, 0.035), "hot", 12, 16),
            torus(f"INSTRUMENT_{i}_COLLAR", (x, 0.4, z), 0.31 * scale, 0.036, "steel"),
        ]
    branches.append(merge("GLASS_AND_INSTRUMENTS", optical))

    panel = box("PANEL_PLATE", (0.52, 0.055, 0.36), (0, 0, 0), "steel")
    bolt = sphere("PANEL_BOLT", (0, 0.045, 0), (0.035, 0.025, 0.035), "brass", 7, 12)
    bolt_targets = node("grid", "BOLT_POSITIONS", type=4, orient=2, rows=2, cols=2, size=(0.38, 0.24))
    prototype = merge(
        "BOLTED_PANEL_PROTOTYPE",
        [
            panel,
            copy("FOUR_BOLTS", bolt, bolt_targets),
            box("PANEL_STATUS_LIGHT", (0.26, 0.025, 0.055), (0, 0.035, 0), "glow"),
        ],
    )
    targets = node("grid", "INSTANCE_GRID", type=4, orient=2, rows=7, cols=9, size=(8.64, 6.24), t=(0, 0.015, 0))
    targets = wrangle(
        "STABLE_INSTANCE_POINTS",
        targets,
        """
int x=int(rint(@P.x/1.08))+4; int z=int(rint(@P.z/1.04))+3;
if(abs(x-4)<2 && abs(z-3)<2) removepoint(0,@ptnum);
i@id=1000+z*9+x; v@N={0,0,1}; v@up={0,1,0};
""",
    )
    instances = copy("PACKED_BOLTED_PANELS", prototype, targets, True)
    instances.parm("targetattribs").set(1)
    instances.parm("applyattribs1").set("id")
    branches.append(
        wrangle(
            "PERSISTENT_PACKED_IDENTITIES",
            instances,
            'int identity=point(0,"id",primpoint(0,@primnum,0)); s@name=sprintf("courtyard_panel_%d",identity);',
            1,
        )
    )

    strand = node("line", "VEGETATION_LINE", points=12)
    strand = wrangle("BEND_AND_TAPER_CURVES", strand, "float t=@P.y; @P.x=.16*t*t; f@width=.014*(1-t)+.002;")
    strand = bind(strand, "grass", "VEGETATION_MATERIAL")
    strands = points(
        "CURVE_VEGETATION_AND_CABLES",
        260,
        """
float a=rand(@ptnum+410.1)*2*M_PI; float r=fit01(rand(@ptnum+410.2),2.6,4.9);
@P=set(r*cos(a),-.05,r*.74*sin(a));
v@scale=set(1,fit01(rand(@ptnum+410.3),.20,.55),1);
p@orient=quaternion(a,{0,1,0});
""",
    )
    parameter(strands, "count", "Vegetation Curves", 260, 20, 600)
    strands.inputs()[0].parm("points").setExpression(
        'ch("../CURVE_VEGETATION_AND_CABLES/count")', hou.exprLanguage.Hscript
    )
    vegetation = copy("NATIVE_VEGETATION", strand, strands)
    cable = node("line", "CABLE_LINE", points=64)
    cable = wrangle(
        "CABLE_SAG", cable, "float t=@P.y; @P=set(-3.1+6.2*t,.5+.5*sin(t*M_PI),-1.7+.1*sin(t*6*M_PI)); f@width=.025;"
    )
    cable = bind(cable, "steel", "CABLE_MATERIAL")
    cable_targets = points("CABLE_OFFSETS", 5, "@P=set(0,.18*@ptnum,0);")
    curves = merge("NATIVE_VEGETATION_AND_CABLES", [vegetation, copy("FIVE_CABLES", cable, cable_targets)])
    branches.append(wrangle("STABLE_CURVE_IDS", curves, "i@id=@primnum;", 1))

    particles = points(
        "SPARK_PARTICLES",
        360,
        """
float a=rand(@ptnum+610.1)*2*M_PI; float r=fit01(rand(@ptnum+610.2),.1,1.1);
@P=set(-1.7+r*cos(a),fit01(rand(@ptnum+610.3),.6,3.8),-.35+r*sin(a));
i@id=@ptnum; f@pscale=fit01(rand(@ptnum+610.4),.008,.018);
v@Cd=set(1,fit01(rand(@ptnum+610.5),.14,.6),.015); v@v={.3,2,.1};
""",
    )
    parameter(particles, "count", "Spark Count", 360, 10, 1200)
    particles.inputs()[0].parm("points").setExpression('ch("../SPARK_PARTICLES/count")', hou.exprLanguage.Hscript)
    branches.append(
        wrangle("SPARK_MATERIAL", particles, "s@shop_materialpath=" + json.dumps(materials["sparks"].path()) + ";", 0)
    )

    accent = node("python", "PYTHON_TEST_ACCENT")
    accent.parm("python").set("""import hou
g=hou.pwd().geometry()
points=[]
for position in ((0,4.3,0),(-.12,4.0,-.12),(.12,4.0,-.12),(.12,4.0,.12),(-.12,4.0,.12)):
    point=g.createPoint();point.setPosition(position);points.append(point)
for indices in ((0,1,2),(0,2,3),(0,3,4),(0,4,1),(1,4,3,2)):
    face=g.createPolygon()
    for index in indices:face.addVertex(points[index])
""")
    branches.append(surface(accent, "glow", "PYTHON_ACCENT_MATERIAL"))
    return dict(branches=branches, ground=ground, hero=hero, rocks=rocks, strands=strands, particles=particles)
