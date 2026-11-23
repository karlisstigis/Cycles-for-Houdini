# SPDX-License-Identifier: Apache-2.0
"""Tiny real Pyro SOP and POP DOP networks for observer regressions."""

import hou


def create_solvers():
    hou.setFps(24)
    hou.setFrame(1)
    root = hou.node("/obj").createNode("subnet", "solver_idle_fixture")
    pyro_geo = root.createNode("geo", "pyro", run_init_scripts=False)
    data = hou.Geometry()
    data.addAttrib(hou.attribType.Prim, "name", "")
    density = data.createVolume(4, 4, 4, hou.BoundingBox(-0.5, -0.5, -0.5, 0.5, 0.5, 0.5))
    density.setAttribValue("name", "density")
    density.setAllVoxels([1.0] * 64)
    source = pyro_geo.createNode("stash", "density_source")
    source.parm("stash").set(data)
    pyro = pyro_geo.createNode("pyrosolver", "pyro_solver")
    pyro.setInput(0, source)
    pyro.setParms(dict(divsize=0.25, substeps=1, minimumsubsteps=1, sparse_maxpadding=0.25))
    pyro.setDisplayFlag(True)
    pyro.setRenderFlag(True)

    pop_geo = root.createNode("geo", "pop", run_init_scripts=False)
    pop = pop_geo.createNode("dopnet", "pop_network")
    particles = pop.createNode("popobject", "particles")
    solver = pop.createNode("popsolver", "pop_solver")
    emitter = pop.createNode("poplocation", "source")
    emitter.setParms(dict(constantrate=48, impulserate=0, velx=0.5, life=10))
    solver.setInput(0, particles)
    solver.setInput(2, emitter)
    output = pop.node("file_output")
    output.setInput(0, solver)
    output.setDisplayFlag(True)
    pop.setDisplayFlag(True)
    pop.setRenderFlag(True)
    return root, [("Pyro", pyro_geo, pyro, pyro.parm("timescale")), ("POP", pop_geo, pop, pop.parm("timescale"))]
