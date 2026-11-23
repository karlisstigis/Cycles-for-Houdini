# SPDX-License-Identifier: Apache-2.0
"""Owned coarse polygons, dicing, crease mapping, attributes and native updates."""

from support.paths import artifacts_root
from subdivision_test_setup import configure_render, set_subdivision
from cycles_houdini.device_selection import set_devices as set_rop_devices
import json
import math
import os
from pathlib import Path
import hou
import numpy as np
import cycles_houdini as ch
from cycles_houdini import native_scene, passes

root = Path(os.environ["CYCLES_HOUDINI_SOURCE_ROOT"])
destination = artifacts_root() / "stage2/subdivision"
destination.mkdir(parents=True, exist_ok=True)
hou.setFrame(1)
obj = hou.node("/obj").createNode("geo", "subdivision_receiver", run_init_scripts=False)
camera = hou.node("/obj").createNode("cam", "subdivision_camera")
camera.parm("tz").set(5)
camera.parm("projection").set(1)
camera.parm("orthowidth").set(4)
material = dict(key="surface", kind="emission", color=(0.2, 0.4, 0.8), strength=1)
builder = ch.create_material(name="subdivision_surface", shader="emission")
builder.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
builder.node("surface_shader").parm("strength").set(1)
obj.parm("shop_materialpath").set(builder.path())


def polygon_sop(name, positions, faces, attributes=False, crease_owner="vertex"):
    sop = obj.createNode("python", name)
    for key in ("sharpness", "corner"):
        sop.addSpareParmTuple(hou.FloatParmTemplate(key, key, 1, default_value=(0.0,)))
    sop.parm("python").set(
        """import hou
g=hou.pwd().geometry()
g.addAttrib(hou.attribType.Point,'id',0)
g.addAttrib(hou.attribType.Point,'v',(24.,0.,0.))
crease_type=hou.attribType.Vertex if %r=='vertex' else hou.attribType.Prim
g.addAttrib(crease_type,'creaseweight',0.)
g.addAttrib(hou.attribType.Point,'cornerweight',0.)
points=[]
for index,position in enumerate(%r):
    point=g.createPoint();point.setPosition(position);point.setAttribValue('id',index)
    point.setAttribValue('cornerweight',hou.pwd().evalParm('corner') if index==0 else 0.);points.append(point)
if %r:
    g.addAttrib(hou.attribType.Vertex,'uv',(0.,0.,0.));g.addAttrib(hou.attribType.Vertex,'Cd',(0.,0.,0.))
for indices in %r:
    polygon=g.createPolygon()
    if crease_type==hou.attribType.Prim:polygon.setAttribValue('creaseweight',hou.pwd().evalParm('sharpness'))
    for corner,index in enumerate(indices):
        vertex=polygon.addVertex(points[index])
        if crease_type==hou.attribType.Vertex:vertex.setAttribValue('creaseweight',hou.pwd().evalParm('sharpness'))
        if %r:
            u,v=[(0.,0.),(1.,0.),(1.,1.),(0.,1.)][corner]
            vertex.setAttribValue('uv',(u,v,0.));vertex.setAttribValue('Cd',(u,v,.25))
"""
        % (crease_owner, positions, attributes, faces, attributes)
    )
    sop.geometry()
    assert not sop.errors(), sop.errors()
    return sop


quad = polygon_sop(
    "coarse_quad", [(-0.5, -0.5, 0), (0.5, -0.5, 0), (0.5, 0.5, 0), (-0.5, 0.5, 0)], [(0, 1, 2, 3)], True
)
cube = polygon_sop(
    "coarse_cube",
    [
        (-0.7, -0.7, -0.7),
        (0.7, -0.7, -0.7),
        (0.7, 0.7, -0.7),
        (-0.7, 0.7, -0.7),
        (-0.7, -0.7, 0.7),
        (0.7, -0.7, 0.7),
        (0.7, 0.7, 0.7),
        (-0.7, 0.7, 0.7),
    ],
    [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)],
)
primitive_cube = polygon_sop(
    "primitive_crease_cube",
    [tuple(p.position()) for p in cube.geometry().points()],
    [[v.point().number() for v in p.vertices()] for p in cube.geometry().prims()],
    crease_owner="primitive",
)
primitive_cube.parm("sharpness").set(1)
ngon = polygon_sop(
    "coarse_ngon",
    [(math.cos(a * 2 * math.pi / 5) * 0.6, math.sin(a * 2 * math.pi / 5) * 0.6, 0.2) for a in range(5)],
    [(0, 1, 2, 3, 4)],
)
seams = polygon_sop(
    "coarse_seams",
    [(-1, -0.5, 0), (0, -0.5, 0), (1, -0.5, 0), (-1, 0.5, 0), (0, 0.5, 0), (1, 0.5, 0)],
    [(0, 1, 4, 3), (1, 2, 5, 4)],
    True,
)
soup = obj.createNode("polysoup", "coarse_soup")
soup.setInput(0, cube)
refined = obj.createNode("subdivide", "independent_houdini_crease_reference")
refined.setInput(0, cube)
refined.parm("algorithm").set("osdcc")
refined.parm("iterations").set(5)
fvar_cube = obj.createNode("python", "coarse_fvar_cube")
fvar_cube.setInput(0, cube)
fvar_cube.parm("python").set(
    "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Vertex,'Cd',(0.,0.,0.))\nfor p in g.prims():\n    for v,color in zip(p.vertices(),[(0.,0.,.25),(1.,0.,.25),(1.,1.,.25),(0.,1.,.25)]):v.setAttribValue('Cd',(color[0]+p.number()*.02,color[1]+p.number()*.02,color[2]))"
)
fvar_reference = obj.createNode("subdivide", "independent_houdini_fvar_reference")
fvar_reference.setInput(0, fvar_cube)
fvar_reference.parm("algorithm").set("osdcc")
fvar_reference.parm("iterations").set(6)
fvar_reference.parm("fvarlinear").set("none")
affine_cube = obj.createNode("python", "coarse_affine_cube")
affine_cube.setInput(0, cube)
affine_cube.parm("python").set(
    "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Vertex,'Cd',(0.,0.,0.))\nfor p in g.prims():\n    for v in p.vertices():\n        P=v.point().position();v.setAttribValue('Cd',((P[0]+.7)/1.4,(P[1]+.7)/1.4,.25))"
)
moving = obj.createNode("xform", "moving_control_mesh")
moving.setInput(0, quad)
moving.parm("tx").setExpression("$T*24", hou.exprLanguage.Hscript)
scaled_quad = obj.createNode("xform", "baked_scaled_quad")
scaled_quad.setInput(0, quad)
scaled_quad.parm("scale").set(2)
changing_crease = obj.createNode("python", "changing_control_creases")
changing_crease.setInput(0, cube)
changing_crease.parm("python").set(
    "import hou\ng=hou.pwd().geometry()\nfor p in g.prims():\n    for v in p.vertices():v.setAttribValue('creaseweight',abs(hou.time()))"
)
changing_faces = obj.createNode("python", "changing_control_faces")
changing_faces.setInput(0, cube)
changing_faces.parm("python").set(
    "import hou\ng=hou.pwd().geometry()\nif hou.time()<0:g.deletePrims([g.prims()[0]],False)"
)
obj.parmTuple("t").set((0.3, 0.1, 1.5))
obj.parmTuple("r").set((20, 35, 17))
obj.parmTuple("s").set((1.5, 0.6, 1.2))
baked_matrix = tuple(obj.worldTransform().asTuple())
baked_cube = obj.createNode("python", "baked_equivalent_control_mesh")
baked_cube.setInput(0, cube)
baked_cube.parm("python").set(
    "import hou\ng=hou.pwd().geometry();M=hou.Matrix4(%r)\nfor p in g.points():p.setPosition(p.position()*M)"
    % (baked_matrix,)
)
obj.parmTuple("t").set((0, 0, 0))
obj.parmTuple("r").set((0, 0, 0))
obj.parmTuple("s").set((1, 1, 1))
targets = obj.createNode("python", "instance_targets")
targets.parm("python").set("""import hou
g=hou.pwd().geometry();g.addAttrib(hou.attribType.Point,'pscale',1.)
for x,scale in [(-1.,.5),(1.,1.)]:
    p=g.createPoint();p.setPosition((x,0,0));p.setAttribValue('pscale',scale)
""")
packed = obj.createNode("copytopoints", "packed_control_cubes")
packed.setInput(0, cube)
packed.setInput(1, targets)
packed.parm("pack").set(1)
rop = hou.node("/out").createNode("cycles_ipr", "native_subdivision_cycles")
configure_render(
    rop,
    dict(
        source_mode="sop",
        soppath=cube.path(),
        camera=camera.path(),
        resx=160,
        resy=90,
        samples=16,
        material_kind="emission",
        subdivision=dict(type="catmull_clark", space="object", dicing_rate=0.025, max_level=7),
    ),
)
rop.parmTuple("basecolor").set((0.2, 0.4, 0.8))
rop.parmTuple("background").set((0, 0, 0))


def planes(frame, name):
    data = frame["planes"][name]
    return np.frombuffer(data["pixels"], dtype=np.float32).reshape(frame["height"], frame["width"], data["channels"])


def request(sop=cube, **settings):
    options = dict(type="catmull_clark", space="object", dicing_rate=0.025, max_level=7)
    options.update(settings)
    return ch.geometry_request(obj, sop, material="surface", subdivision=options)


def render(session, sop=cube, **settings):
    return session.wait(
        session.apply(
            dict(
                geometry=[request(sop, **settings)],
                materials=[material],
                camera_path=camera.path(),
                background=(0, 0, 0),
            )
        )
    )


def z_at(frame, x=0):
    return float(planes(frame, "position")[45, round((x / 4 + 0.5) * frame["width"]), 2])


def reject(session, scene, text):
    before = session.status()
    try:
        session.wait(session.apply(scene))
    except (ValueError, RuntimeError) as error:
        assert text in str(error), str(error)
    else:
        raise AssertionError("Accepted invalid subdivision: " + text)
    after = session.status()
    key = "committed_generation" if "committed_generation" in before else "generation"
    assert after[key] == before[key]


reports = []
for device in ("CPU", ch.default_device()):
    prefix = "cpu" if device == "CPU" else "optix"
    checks = []
    camera.parm("tx").set(0)
    cube.parm("sharpness").set(0)
    cube.parm("corner").set(0)
    with ch.Session(device=device, width=160, height=90, samples=64, collect_diagnostics=True) as session:
        frame = render(session)
        status = session.status()
        assert status["subdivision_control_faces"] == 6 and status["subdivision_triangles"] > 12, status
        assert 0.45 < z_at(frame) < 0.6, z_at(frame)
        session.write_exr(str(destination / (prefix + "-cube.exr")))
        checks.append(
            dict(name="Original six quads produce a rounded Catmull-Clark cube", status=status, centre_z=z_at(frame))
        )
        camera.parm("tx").set(0.1)
        session.wait(session.apply(dict(camera_path=camera.path())))
        after = session.status()
        assert (
            after["geometry_conversions"] == status["geometry_conversions"]
            and after["subdivision_tessellations"] == status["subdivision_tessellations"]
        )
        checks.append(dict(name="Object-space camera edits retain control data and tessellation", status=after))
        camera.parm("tx").set(0)
        frame = render(session, cube, type="linear")
        assert abs(z_at(frame) - 0.7) < 0.001
        checks.append(dict(name="Linear mode preserves the cube face", status=session.status()))
        cube.parm("sharpness").set(10)
        frame = render(session)
        assert abs(z_at(frame) - 0.7) < 0.001
        checks.append(dict(name="Houdini sharpness 10 maps to OpenSubdiv sharpness 10", status=session.status()))
        cube.parm("sharpness").set(1)
        native_crease = render(session)
        primitive_crease = render(session, primitive_cube)
        assert np.array_equal(planes(native_crease, "combined"), planes(primitive_crease, "combined"))
        assert np.array_equal(planes(native_crease, "position"), planes(primitive_crease, "position"))
        checks.append(
            dict(name="Primitive creaseweight fallback matches equivalent vertex creases", status=session.status())
        )
        reference_crease = render(session, refined, type="none")
        mask = (planes(native_crease, "combined")[:, :, 0] > 0.1999) & (
            planes(reference_crease, "combined")[:, :, 0] > 0.1999
        )
        difference = float(
            np.max(
                np.abs(
                    planes(native_crease, "position")[:, :, 2][mask]
                    - planes(reference_crease, "position")[:, :, 2][mask]
                )
            )
        )
        assert difference < 0.01, difference
        rgba_mean = float(np.mean(np.abs(planes(native_crease, "combined") - planes(reference_crease, "combined"))))
        assert rgba_mean < 0.002, rgba_mean
        checks.append(
            dict(
                name="Sharpness one agrees with independent Houdini OpenSubdiv refinement",
                maximum_interior_z_difference=difference,
                mean_rgba_difference=rgba_mean,
            )
        )
        cube.parm("sharpness").set(0)
        frame = render(session)
        assert 0.45 < z_at(frame) < 0.6
        checks.append(dict(name="Removing creases restores the original limit surface", status=session.status()))
        cube.parm("corner").set(10)
        render(session)
        cube.parm("corner").set(0)
        render(session)
        checks.append(dict(name="Vertex creases can be added and retired", status=session.status()))
        frame = render(session, soup)
        assert 0.45 < z_at(frame) < 0.6 and session.status()["subdivision_control_faces"] == 6
        checks.append(dict(name="Polygon soup traverses six separate control faces", status=session.status()))
        frame = render(session, ngon, type="linear")
        assert abs(z_at(frame) - 0.2) < 0.001
        checks.append(dict(name="Original five-corner face produces linear n-gon patches", status=session.status()))
        frame = render(session, ngon)
        assert abs(z_at(frame) - 0.2) < 0.001 and session.status()["subdivision_control_faces"] == 1
        checks.append(dict(name="Catmull-Clark evaluates the original five-corner face", status=session.status()))
        frame = render(session, quad, type="linear", fvar="all")
        assert abs(z_at(frame)) < 0.001
        uv = planes(frame, "uv")[45, 80, :2]
        assert np.max(np.abs(uv - (0.5125, 0.5125))) < 0.015, uv
        checks.append(dict(name="Coarse UV uses float2 storage and face-varying interpolation", uv=uv.tolist()))
        frame = render(session, quad, boundary="edge_only")
        rounded_coverage = float(np.sum(planes(frame, "combined")[:, :, 0]))
        frame = render(session, quad, boundary="edge_and_corner")
        sharp_coverage = float(np.sum(planes(frame, "combined")[:, :, 0]))
        assert rounded_coverage < sharp_coverage * 0.95, (rounded_coverage, sharp_coverage)
        render(session, cube, boundary="none")
        recovered = render(session, quad, boundary="none")
        issues = session.status()["source_diagnostics"]
        assert issues and any("closed" in issue["message"].lower() for issue in issues), issues
        assert np.isfinite(planes(recovered, "position")).all() and planes(recovered, "combined")[:, :, 3].max() > 0
        render(session, quad, boundary="edge_and_corner")
        assert not session.status()["source_diagnostics"]
        assert np.array_equal(planes(recovered, "position"), planes(session.frame(), "position"))
        checks.append(
            dict(
                name="Unsupported open-boundary subdivision retains visible geometry and repairs without rejection",
                rounded_coverage=rounded_coverage,
                sharp_coverage=sharp_coverage,
                diagnostics=issues,
            )
        )
        colored = dict(material, color=(1, 1, 1), color_attribute="Cd")
        frame = session.wait(
            session.apply(dict(geometry=[request(seams, fvar="all")], materials=[colored], camera_path=camera.path()))
        )
        for column in (60, 100):
            color = planes(frame, "combined")[45, column, :3]
            assert np.max(np.abs(color - (0.5125, 0.5125, 0.25))) < 0.02, color
        checks.append(dict(name="Connected control faces retain independent UV and Cd seams", status=session.status()))
        native_fvar = session.wait(
            session.apply(
                dict(geometry=[request(fvar_cube, fvar="none")], materials=[colored], camera_path=camera.path())
            )
        )
        reference_fvar = session.wait(
            session.apply(
                dict(geometry=[request(fvar_reference, type="none")], materials=[colored], camera_path=camera.path())
            )
        )
        # Blue is a constant .25 on every face, so it identifies fully covered
        # pixels independently of the varying red/green field.
        mask = (planes(native_fvar, "combined")[:, :, 2] > 0.2499) & (
            planes(reference_fvar, "combined")[:, :, 2] > 0.2499
        )
        color_difference = float(
            np.max(
                np.abs(
                    planes(native_fvar, "combined")[:, :, :3][mask] - planes(reference_fvar, "combined")[:, :, :3][mask]
                )
            )
        )
        if color_difference >= 0.025:
            for label, frame in (("native-fvar", native_fvar), ("reference-fvar", reference_fvar)):
                np.save(destination / (prefix + "-" + label + ".npy"), planes(frame, "combined"))
            index = np.argmax(
                np.where(
                    mask[:, :, None],
                    np.abs(planes(native_fvar, "combined")[:, :, :3] - planes(reference_fvar, "combined")[:, :, :3]),
                    -1,
                )
            )
            row, column, channel = np.unravel_index(index, (90, 160, 3))
            print(
                "Fvar mismatch",
                row,
                column,
                channel,
                planes(native_fvar, "combined")[row, column].tolist(),
                planes(reference_fvar, "combined")[row, column].tolist(),
            )
        assert color_difference < 0.025, color_difference
        color_mean = float(
            np.mean(
                np.abs(
                    planes(native_fvar, "combined")[:, :, :3][mask] - planes(reference_fvar, "combined")[:, :, :3][mask]
                )
            )
        )
        assert color_mean < 0.001, color_mean
        checks.append(
            dict(
                name="Discontinuous smooth Cd agrees with six-level independent Houdini refinement",
                reference_subdivision_levels=6,
                samples=64,
                maximum_interior_color_difference=color_difference,
                mean_interior_color_difference=color_mean,
            )
        )
        affine = session.wait(
            session.apply(
                dict(geometry=[request(affine_cube, fvar="none")], materials=[colored], camera_path=camera.path())
            )
        )
        mask = planes(affine, "combined")[:, :, 2] > 0.2499
        analytic = (planes(affine, "position")[:, :, :2] + 0.7) / 1.4
        affine_difference = float(np.max(np.abs(planes(affine, "combined")[:, :, :2][mask] - analytic[mask])))
        assert affine_difference < 0.002, affine_difference
        checks.append(
            dict(
                name="Continuous affine Cd follows the independently specified limit-position relationship",
                maximum_interior_color_difference=affine_difference,
            )
        )
        seam_builders = []
        for index, height in enumerate((0.1, 0.5)):
            b = ch.create_material(name="subdivision_seam_" + prefix + str(index), shader="emission")
            b.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
            b.node("surface_shader").parm("strength").set(1)
            offset = b.createNode("cycles::vector_displacement::1")
            # The default this test was written for (new nodes now take Blender's).
            offset.setParms(dict(scale=1.0, space="object"))
            offset.parmTuple("vector").set((0, 0, height))
            b.node("surface_output").setNamedInput("displacement", offset, "displacement")
            b.parm("cycles_displacement_method").set("true")
            seam_builders.append(b)
        assigned = obj.createNode("python", "assigned_subdivision_" + prefix)
        assigned.setInput(0, seams)
        assigned.parm("python").set(
            "import hou\ng=hou.pwd().geometry();g.addAttrib(hou.attribType.Prim,'shop_materialpath','')\nfor p,path in zip(g.prims(),%r):p.setAttribValue('shop_materialpath',path)"
            % [b.path() for b in seam_builders]
        )
        native = ch.geometry_request(
            obj,
            assigned,
            material="surface",
            material_mode="native",
            subdivision=dict(type="catmull_clark", space="object", dicing_rate=0.025, max_level=7, fvar="all"),
        )
        frame = session.wait(
            session.apply(
                dict(
                    geometry=[native],
                    materials=[material],
                    camera_path=camera.path(),
                    material_resolver=native_scene.resolve_materials,
                )
            )
        )
        assert abs(z_at(frame, -0.5) - 0.1) < 0.001 and abs(z_at(frame, 0.5) - 0.5) < 0.001
        checks.append(
            dict(
                name="Displacement seams split generated vertices and preserve the control surface",
                status=session.status(),
            )
        )
        render(session, cube, space="pixel", dicing_rate=4, max_level=7)
        before = session.status()
        recolored = dict(material, color=(0.3, 0.5, 0.7))
        session.wait(
            session.apply(
                dict(
                    geometry=[request(cube, space="pixel", dicing_rate=4, max_level=7)],
                    materials=[recolored],
                    camera_path=camera.path(),
                )
            )
        )
        unchanged = session.status()
        assert (
            unchanged["geometry_conversions"] == before["geometry_conversions"]
            and unchanged["subdivision_tessellations"] == before["subdivision_tessellations"]
        ), (before, unchanged)
        checks.append(
            dict(
                name="Repeated identical camera and surface-only shader edit retain pixel tessellation",
                before=before,
                after=unchanged,
            )
        )
        camera.parm("orthowidth").set(2)
        session.wait(session.apply(dict(camera_path=camera.path())))
        after = session.status()
        assert after["geometry_conversions"] == before["geometry_conversions"]
        assert (
            after["subdivision_tessellations"] > before["subdivision_tessellations"]
            and after["subdivision_triangles"] > before["subdivision_triangles"]
        ), (before, after)
        checks.append(
            dict(name="Pixel dicing follows camera changes without SOP conversion", before=before, after=after)
        )
        camera.parm("orthowidth").set(4)
        obj.parmTuple("s").set((2, 2, 2))
        render(session, quad, type="linear", space="pixel", dicing_rate=4)
        transformed = session.status()["subdivision_triangles"]
        obj.parmTuple("s").set((1, 1, 1))
        render(session, scaled_quad, type="linear", space="pixel", dicing_rate=4)
        baked = session.status()["subdivision_triangles"]
        assert transformed == baked, (transformed, baked)
        checks.append(
            dict(
                name="Equivalent world-space surfaces dice equally with OBJ versus baked SOP scale",
                transformed_triangles=transformed,
                baked_triangles=baked,
            )
        )
        camera.parm("projection").set(0)
        obj.parmTuple("t").set((0.3, 0.1, 1.5))
        obj.parmTuple("r").set((20, 35, 17))
        obj.parmTuple("s").set((1.5, 0.6, 1.2))
        transformed_frame = render(session, cube, space="pixel", dicing_rate=2)
        transformed = session.status()["subdivision_triangles"]
        obj.parmTuple("t").set((0, 0, 0))
        obj.parmTuple("r").set((0, 0, 0))
        obj.parmTuple("s").set((1, 1, 1))
        baked_frame = render(session, baked_cube, space="pixel", dicing_rate=2)
        baked = session.status()["subdivision_triangles"]
        assert transformed == baked and np.array_equal(
            planes(transformed_frame, "combined"), planes(baked_frame, "combined")
        ), (transformed, baked)
        checks.append(
            dict(
                name="Perspective dicing matches independently baked rotated nonuniform curved geometry",
                transformed_triangles=transformed,
                baked_triangles=baked,
            )
        )
        camera.parm("projection").set(1)
        render(session, packed, space="object")
        assert session.status()["geometry_resources"] == 1 and session.status()["mesh_variant_resources"] == 1
        render(session, packed, space="pixel", dicing_rate=4)
        assert session.status()["geometry_resources"] == 1 and session.status()["mesh_variant_resources"] == 2
        checks.append(
            dict(
                name="Object-space instances share; pixel instances use transform-compatible variants",
                status=session.status(),
            )
        )
        reject(session, dict(geometry=[request(max_level=17)]), "maximum level")
        reject(session, dict(geometry=[request(dicing_rate=0)]), "dicing rate")
        cube.parm("sharpness").set(-1)
        recovered = render(session)
        assert any(d["code"] == "geometry.invalid_creaseweight" for d in session.status()["source_diagnostics"])
        cube.parm("sharpness").set(0)
        reference = render(session)
        assert not session.status()["source_diagnostics"]
        assert np.array_equal(planes(recovered, "position"), planes(reference, "position"))
        checks.append(
            dict(
                name="Invalid settings remain strict; invalid source creases render smooth and repair",
                status=session.status(),
            )
        )
        warm = []
        for i in range(16):
            render(session, cube, type="linear" if i % 2 else "catmull_clark")
            current = session.status()
            if i >= 4:
                warm.append(
                    (
                        i % 2,
                        current["owned_geometry_bytes"],
                        current["device_allocation_bytes"],
                        current["subdivision_triangles"],
                    )
                )
        for phase in (0, 1):
            values = [item for item in warm if item[0] == phase]
            assert len({(item[1], item[3]) for item in values}) == 1, warm
            # Output arrival precedes release of some engine transient buffers.
            # Require stable owned payload/counts and no increasing allocation peak.
            assert max(item[2] for item in values[-3:]) <= max(item[2] for item in values[:3]), warm
        checks.append(dict(name="Repeated mode edits retain bounded tracked payloads", warm=warm))
        native = request(moving)
        native["motion"] = "deformation"
        session.wait(
            session.apply(
                dict(
                    geometry=[native],
                    materials=[material],
                    camera_path=camera.path(),
                    motion=dict(enabled=True, fps=24, shutter_frames=1, offset=0, samples=3),
                )
            )
        )
        assert session.status()["motion_samples"] == 3 and session.status()["subdivision_triangles"] > 2
        checks.append(
            dict(name="Stable original quad connectivity interpolates deformation samples", status=session.status())
        )
        for sop, text in (
            (changing_crease, "Subdivision topology/settings"),
            (changing_faces, "Subdivision topology/settings"),
        ):
            native = request(sop)
            native["motion"] = "deformation"
            recovered = session.wait(
                session.apply(
                    dict(
                        geometry=[native],
                        materials=[material],
                        camera_path=camera.path(),
                        motion=dict(enabled=True, fps=24, shutter_frames=1, offset=0, samples=3),
                    )
                )
            )
            assert any(
                d["code"] == "motion.unavailable" and text in d["message"]
                for d in session.status()["source_diagnostics"]
            )
            reference = session.wait(session.apply(dict(geometry=[request(sop)], motion=dict(enabled=False))))
            assert np.array_equal(planes(recovered, "position"), planes(reference, "position"))
        checks.append(
            dict(
                name="Changing control faces and crease state use requested-frame geometry without deformation blur",
                status=session.status(),
            )
        )
        session.cancel()
        session.close()
        assert session.status()["owned_geometry_bytes"] == 0 and session.status()["device_allocation_bytes"] == 0
        checks.append(
            dict(name="Cancellation and close retire control, variant and generated buffers", status=session.status())
        )
    reports.append(dict(device=device, result="passed", checks=checks))
cube.setDisplayFlag(True)
cube.setRenderFlag(True)
set_rop_devices(rop, ch.default_device())
rop.parm("picture").set(str(destination / "subdivision.$F4.exr"))
hou.setFrame(1)
passes.add(rop, "depth")
hou.hipFile.save(str(destination / "native-subdivision.hiplc"))
import OpenImageIO as OIIO

batch_material = ch.create_material(name="subdivision_batch_surface", shader="emission")
batch_material.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
batch_material.node("surface_shader").parm("strength").set(1)
offset = batch_material.createNode("cycles::vector_displacement::1")
# The default this test was written for (new nodes now take Blender's).
offset.setParms(dict(scale=1.0, space="object"))
offset.parmTuple("vector").set((0.0, 0.0, 0.0))
offset.parmTuple("vector")[2].setExpression("$F*.1", hou.exprLanguage.Hscript)
batch_material.node("surface_output").setNamedInput("displacement", offset, "displacement")
batch_material.parm("cycles_displacement_method").set("true")
set_rop_devices(rop, "CPU")
rop.parm("soppath").set(quad.path())
rop.parm("materialpath").set(batch_material.path())
rop.parm("picture").set(str(destination / "subdivision-frame.$F4.exr"))
rop.render(frame_range=(1, 3, 1))
batch = []
for number in (1, 2, 3):
    image = OIIO.ImageBuf(str(destination / f"subdivision-frame.{number:04}.exr"))
    depth = float(image.get_pixels()[45, 80, list(image.spec().channelnames).index("depth.Z")])
    assert abs(depth - (5 - number * 0.1)) < 0.001, (number, depth)
    batch.append(dict(frame=number, depth=depth, expected=5 - number * 0.1))
(destination / "houdini-subdivision-smoke.json").write_text(
    json.dumps(dict(result="passed", reports=reports, batch=batch), indent=2)
)
print(destination / "houdini-subdivision-smoke.json")
print(json.dumps(dict(result="passed", devices=len(reports), batch_frames=len(batch))))
