# SPDX-License-Identifier: Apache-2.0
"""Native SOP/camera controls for independent lens-motion and recovery tests."""

from subdivision_test_setup import configure_render, set_subdivision
import pass_fixtures as pass_fixture

from cycles_houdini.device_selection import set_devices as set_rop_devices
import hou
import cycles_houdini as ch
from cycles_houdini import camera_properties

CASES = (
    "focus_pull",
    "zoom",
    "ortho_scale",
    "clipping",
    "offframe_invalid_focus",
    "invalid_nominal_focus",
    "projection_switch",
    "offframe_invalid_focal",
)


class Fixture:
    def __init__(self, device):
        hou.setFps(24)
        hou.setFrame(1)
        self.material = ch.create_material(name="camera_motion_material", shader="emission")
        self.material.node("surface_shader").parmTuple("color").set((0.2, 0.4, 0.8))
        pass_fixture.parm(self.material.node("surface_shader"), "strength").set(1)
        self.obj = hou.node("/obj").createNode("geo", "camera_motion_subject", run_init_scripts=False)
        pass_fixture.parm(self.obj, "shop_materialpath").set(self.material.path())
        self.grid = self.obj.createNode("grid", "source_quad")
        pass_fixture.set_parameters(self.grid, dict(type=0, orient=0, rows=2, cols=2))
        self.grid.parmTuple("size").set((1.4, 1))
        reverse = self.obj.createNode("reverse", "front_faces")
        reverse.setInput(0, self.grid)
        self.sop = self.obj.createNode("attribwrangle", "moving_surface")
        self.sop.setInput(0, reverse)
        pass_fixture.parm(self.sop, "snippet").set("@P.x+=@Time*2;")
        self.sop.setDisplayFlag(True)
        self.sop.setRenderFlag(True)
        self.camera = hou.node("/obj").createNode("cam", "lens_motion_camera")
        camera_properties.install(self.camera)
        self.rop = hou.node("/out").createNode("cycles_ipr", "camera_lens_motion")
        set_rop_devices(self.rop, device)
        configure_render(
            self.rop,
            dict(
                source_mode="objects",
                vobject=self.obj.path(),
                camera=self.camera.path(),
                resx=160,
                resy=96,
                samples=1024,
                ipr_samples=1024,
                adaptive_sampling=0,
                ipr_adaptive_sampling=0,
                use_denoise=0,
                ipr_use_denoise=0,
                allowmotionblur=1,
                geometry_motion="deformation",
                motion_samples=3,
                use_material_assignments=1,
                pass_position=1,
                pass_depth=1,
            ),
        )
        self.rop.parmTuple("background").set((0, 0, 0))
        self.configure("valid")

    def configure(self, case):
        self.case = case
        self.camera.setInput(0, None)
        pass_fixture.parm(self.sop, "snippet").set("@P.x+=@Time*2;")
        for name in (
            "focal",
            "focus",
            "fstop",
            "near",
            "far",
            "projection",
            "orthowidth",
            "tx",
            "ty",
            "tz",
            "sx",
            "sy",
            "sz",
            "cycles_aperture_ratio",
            "cycles_aperture_rotation",
            "cycles_aperture_blades",
        ):
            pass_fixture.parm(self.camera, name).deleteAllKeyframes()
        pass_fixture.set_parameters(
            self.camera,
            dict(
                focal=50,
                aperture=200 / 3,
                focus=2.5,
                fstop=0.05,
                near=0.01,
                far=1000,
                projection=0,
                orthowidth=6,
                tx=0,
                ty=0,
                tz=5,
                sx=1,
                sy=1,
                sz=1,
                cycles_dof=0,
                cycles_aperture_ratio=1,
                cycles_aperture_rotation=0,
                cycles_aperture_blades=0,
            ),
        )
        pass_fixture.parm(self.camera, "tx").setExpression("$T*6", hou.exprLanguage.Hscript)
        pass_fixture.parm(self.camera, "cycles_focus_distance").deleteAllKeyframes()
        pass_fixture.parm(self.camera, "cycles_focus_distance").setExpression('ch("focus")', hou.exprLanguage.Hscript)
        configure_render(
            self.rop,
            dict(
                dof_mode="camera",
                shutteroffset=0,
                allowmotionblur=1,
                pass_motion=0,
                vobject=self.obj.path(),
                subdivision=dict(type="none"),
            ),
        )
        if case in (
            "focus_pull",
            "offframe_invalid_focus",
            "invalid_nominal_focus",
            "dof",
            "gui_dof_focus",
            "gui_dof_shift",
        ):
            pass_fixture.parm(self.camera, "cycles_dof").set(1)
            if case == "focus_pull":
                pass_fixture.parm(self.camera, "focus").setExpression("2.5+$T*24", hou.exprLanguage.Hscript)
            elif case == "offframe_invalid_focus":
                pass_fixture.parm(self.camera, "cycles_focus_distance").setExpression(
                    "float('nan') if hou.time()<0 else 2.5", hou.exprLanguage.Python
                )
            elif case == "invalid_nominal_focus":
                pass_fixture.parm(self.camera, "cycles_focus_distance").setExpression(
                    "float('nan')", hou.exprLanguage.Python
                )
            elif case == "gui_dof_focus":
                pass_fixture.parm(self.camera, "focus").set(3.5)
        elif case in ("zoom", "held_transform_zoom", "gui_zoom_shift", "zoom_only"):
            pass_fixture.parm(self.camera, "focal").setExpression("50/(1+$T*24)", hou.exprLanguage.Hscript)
        elif case == "ortho_scale":
            pass_fixture.parm(self.camera, "projection").set(1)
            pass_fixture.parm(self.camera, "orthowidth").setExpression("6+$T*24", hou.exprLanguage.Hscript)
        elif case == "clipping":
            pass_fixture.parm(self.camera, "near").setExpression(".1-$T*4", hou.exprLanguage.Hscript)
        elif case == "projection_switch":
            pass_fixture.parm(self.camera, "projection").setExpression("if($T<0,1,0)", hou.exprLanguage.Hscript)
        elif case == "offframe_invalid_focal":
            pass_fixture.parm(self.camera, "focal").setExpression("if($T<0,0,50)", hou.exprLanguage.Hscript)
        elif case == "quarter_zoom":
            pass_fixture.parm(self.camera, "focal").setExpression("50/(1+.2*sin($T*17280))", hou.exprLanguage.Hscript)
        if case in ("held_transform", "held_transform_zoom"):
            pass_fixture.parm(self.camera, "tx").setExpression(
                "float('nan') if hou.time()<0 else hou.time()*6", hou.exprLanguage.Python
            )
        if case == "zoom_only":
            pass_fixture.parm(self.sop, "snippet").set("")
            pass_fixture.parm(self.camera, "tx").deleteAllKeyframes()
            pass_fixture.parm(self.camera, "tx").set(0)
        if case in ("gui_dof_shift", "gui_zoom_shift"):
            pass_fixture.parm(self.camera, "tx").setExpression(".1+$T*6", hou.exprLanguage.Hscript)

    def witness(self, far=False):
        if not hasattr(self, "witness_obj"):
            self.witness_obj = hou.node("/obj").createNode("geo", "clip_witness", run_init_scripts=False)
            self.witness_material = ch.create_material(name="clip_witness_red", shader="emission")
            self.witness_material.node("surface_shader").parmTuple("color").set((1, 0, 0))
            pass_fixture.parm(self.witness_material.node("surface_shader"), "strength").set(1)
            pass_fixture.parm(self.witness_obj, "shop_materialpath").set(self.witness_material.path())
            self.witness_grid = self.witness_obj.createNode("grid", "clip_quad")
            pass_fixture.set_parameters(self.witness_grid, dict(type=0, orient=0, rows=2, cols=2))
            self.witness_grid.setDisplayFlag(True)
            self.witness_grid.setRenderFlag(True)
        self.witness_grid.parmTuple("size").set((0.2, 0.2) if far else (0.02, 0.02))
        self.witness_obj.parmTuple("t").set((1, 0, -0.11) if far else (0, 0, 4.925))
        pass_fixture.parm(self.rop, "vobject").set(self.obj.path() + " " + self.witness_obj.path())
