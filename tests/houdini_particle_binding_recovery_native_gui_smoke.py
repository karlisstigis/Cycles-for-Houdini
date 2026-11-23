# SPDX-License-Identifier: Apache-2.0
import os
from houdini_particle_binding_recovery_gui_smoke import Workflow

os.environ["CYCLES_HOUDINI_PARTICLE_BINDING_VIEWER"] = "native"
workflow = Workflow()
