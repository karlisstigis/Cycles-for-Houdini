# Authored native material nodes

The integration registers authored Cycles VOPs from the pinned engine socket
definitions. The current expansion covers additional surface shaders,
procedural textures, color/vector/math utilities, and host inputs. Native
subnetworks, parameter promotion, stable graph identity and existing ramps remain
available. This is not full Blender node-tree compatibility.

Most procedural textures with an unconnected Vector use world position, as documented
by the node help. Texture Coordinate Generated explicitly requests normalized
nominal SOP component bounds. Mesh coordinates survive camera movement; point
and curve-root coordinates retain an owned normalized buffer so material edits
need no SOP recook. Generated GPU attributes are allocated only when required by
a bound shader. Degenerate bounds map their missing dimension to zero.
Sky Texture retains Cycles' Generated default: world rays in a background,
normalized SOP bounds on surfaces and explicit coordinates in volumes. Its
authored Sky Rotation uses a native channel name so orientation animation can
survive HIP reload. See [sky controls and evidence](../../user/native-world.md#author-a-procedural-sky).

Scene Time uses Houdini seconds and nominal frame, evaluated at the requested
render time rather than changing the timeline. Color Attribute uses float Cd,
preserving HDR values. Curves Info Length is the control-polyline length in SOP
units; Intercept is normalized distance along the keys. Rounded/cardinal curves
interpolate these inputs using the engine's curve attributes.

Geometry Random Per Island supports connected native mesh components,
subdivision and packed sharing; see [mesh island shading](native-mesh-islands.md).
Pointiness uses original polygon geometry; see `native-mesh-pointiness.md`.
Instancer/reference-object coordinates,
and Blender particle-system metadata require further adapters. IES Texture and
owned analytic-light emission graphs have [their own guide](native-light-shaders.md).
Named fields/layers have their own native attribute guide; world volumes now
have independent absorption/procedural/emission and live-edit checks in
`native-world.md`. Native file HDRIs
and world graphs now have [their own authoring and verification guide](../../user/native-world.md).
OSL remains off
in the certified Houdini dependency configuration. Node registration, individual
image semantics and broad workload certification are recorded separately; a
visible node alone does not prove all of its device/scene combinations.

Legacy texture-mapping parameters are hidden compatibility fields. Existing HIP
values remain active; new coordinate authoring uses Mapping. Removing the old
fields would discard saved values and cause load warnings. On Houdini 22.0.429,
the old dot-named fields do not retain channels through either HOM expression or
keyframe assignment; the isolated probe is `artifacts/legacy-channel-probe.py`.
Use Mapping for animated coordinate transforms. Ordinary texture parameters,
such as Noise Scale, retain their animation across HIP reload.
