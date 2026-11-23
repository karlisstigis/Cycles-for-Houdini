# SPDX-License-Identifier: Apache-2.0
"""Place one precomputed Blender array fixture through the native capture API."""

import json
import hou
import cycles_houdini as ch
from cycles_houdini.blender import blender_assets


def direct_request(obj, sop, record, arrays, material):
    reference = "fixture.blend#object=Mesh"
    sop.parm("snippet").set('int p=addpoint(0,{0,0,0}); setpointattrib(0,"instance",p,' + json.dumps(reference) + ");")
    # Intern the record as a Link import does (Houdini-encoded field names, content key); the
    # fixture arrays are keyed by the record's own array identities, with one material slot.
    key, mesh = blender_assets._native_mesh(record, arrays, {name: name for name in arrays}, 1)
    request = ch.geometry_request(obj, sop, material=material)
    prototype = dict(
        path=reference,
        node_id=0,
        source_reference=reference,
        source_frame=1.0,
        request=dict(request),
        direct_instances=[dict(key="member", geometry_key=key, mesh=mesh)],
    )
    request.update(
        point_instancing=dict(mode="fast", instancer_path=obj.path(), frame=1.0, fallback=-1, prototypes=[prototype]),
        render_types=dict(mesh=False, points=False, curves=False, packed=True, volume=False),
    )
    return request
