# Scene queries and true displacement

The pinned Cycles engine evaluates true displacement before its BVH exists.
Raycast, Ambient Occlusion and Bevel therefore cannot trace the scene at that
stage. Their surface use and ordinary Bump mode remain supported.

The adapter recovers contributing query inputs locally for True and Both.
It clones only affected displacement ancestors, preserving a query shared
with surface shading and keeping the material's assignment and pass identity.
The original owned material remains available for automatic repair.

Scalar Math Add and Vector Math Add retain independently valid terms; an
unavailable term contributes zero to that addition. An explicit unavailable
Normal on scalar Displacement falls back to the geometric normal while
retaining its valid Height. Other coupled operations, such as multiplying
a height by an unavailable query, omit the dependent output. This is a
deliberate boundary: the adapter does not guess missing factors or directions.
If preserving separate branches would exceed the existing graph capacity,
the displacement output is omitted and the diagnostic reports that limit.

Each omitted query publishes `material.displacement_scene_query`, naming
the authored material path, query type (Raycast, Ambient Occlusion or Bevel),
reason and recovery. The current owned graph carries stable node IDs rather
than inner Houdini node paths, so diagnostics identify the material containing
the query. Bare core graphs without an authored material name retain the
original graph node ID. Valid surface shading, other assignments
and independent displacement remain usable. Disconnected queries and queries
disabled by a known radius, length or sample count remain quiet. AO Distance
zero means the integrator's global radius, including a linked Value zero that
Cycles folds into the socket; it is not a disabled query.

Both keeps the recovered displacement graph for its true and bump evaluations.
It is not globally downgraded to Bump, because that would discard valid true
height. Keeping the complete original query for bump while using a different
true-displacement graph would require a separate engine design. Ordinary
Bump mode receives the original graph unchanged.

The focused verification entries are
`houdini_scene_query_displacement_smoke.py` and its two viewer wrappers. They
compare against numeric pre-displaced geometry with matching valid surface
queries, check scalar/vector additions, coupled omission, normal fallback,
disabled/disconnected controls, Bump parity, material index identity, warning
retention and repairs. The headless test also writes five ordered final frames.
The combined runtime `hosts/20261020T204817415/resources` passes 180 accepted
headless checks (176 independent reference images and four camera-only checks)
and twenty ordered final frames across CPU, RTX 4090, RTX 3080 Ti and mixed
OptiX devices. Maximum combined error is below 3.6e-7. Both viewers on CPU and
each GPU additionally pass 144 image states, eighteen repair checks, stable
workers and zero tracked closure bytes. These are sequential programmatic
edits, without a physical-input or queued-navigation latency claim.

The headless analysis (`artifacts/scene-query-displacement-cpu-gpu-analysis-20261020.json`)
and viewer analysis (`artifacts/scene-query-displacement-viewer-analysis-20261020.json`)
retain exact reports, hashes, runtime equality and source-archive references.
