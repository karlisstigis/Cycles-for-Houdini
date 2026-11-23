# SPDX-License-Identifier: Apache-2.0
"""Native packed velocity controls shared by image and live viewer tests."""

import hou
from packed_deformation_fixture import Fixture as DeformationFixture


class Fixture(DeformationFixture):
    def __init__(self, device="CPU"):
        super().__init__(device)
        self.motion.parm("test_speed").set(0)
        self.motion.parm("snippet").set("")
        self.dest = self.dest.parent / "packed-velocity"
        self.dest.mkdir(parents=True, exist_ok=True)
        self.velocity = self.obj.createNode("attribwrangle", "instance_velocities")
        self.velocity.setInput(0, self.copies)
        self.copies = self.velocity
        self.nested.setInput(0, self.copies)
        self.velocity.addSpareParmTuple(
            hou.FloatParmTemplate("test_velocity", "Velocity Scale", 1, default_value=(1.0,))
        )
        self.velocity.addSpareParmTuple(hou.IntParmTemplate("test_invalid", "Velocity Input", 1))
        self.velocity.addSpareParmTuple(hou.ToggleParmTemplate("test_identity", "Duplicate Optional Identities"))
        self.velocity.parm("snippet").set("""v@v=set(48*chf("test_velocity"),0,0);
if(chi("test_invalid")==2 || (chi("test_invalid")==1 && @ptnum==0)) @v.x=atof("nan");
if(chi("test_identity")){i@id=7;s@name="duplicate";}""")
        assert self.copies.geometry().point(0).attribValue("v") == (48.0, 0.0, 0.0)
        # The outer container has no independent velocity. Only its contents do.
        self.outer = self.obj.createNode("attribdelete", "outer_container_without_velocity")
        self.outer.setInput(0, self.nested)
        self.outer.parm("ptdel").set("v")
        self.obj.layoutChildren()

    def rop(self, sop=None, interactive_samples=128):
        result = super().rop(sop or self.outer, interactive_samples)
        result.parm("geometry_motion").set("velocity")
        return result

    def update(self, *args, **kwargs):
        result = super().update(*args, **kwargs)
        for request in result["geometry"]:
            request["motion"] = "velocity"
        return result
