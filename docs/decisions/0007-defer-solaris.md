# Keep OBJ/SOP primary and preserve a reusable scene boundary for Solaris

Accepted 2026-11-10. Invest first in the native Houdini workflow, including the ROP, Render View and interactive worker. Revisit a Solaris/Hydra target in roughly six months, or sooner if adoption warrants it. This is a review horizon, with no committed release date.

Keep Houdini node traversal, SOHO callbacks and viewer state in the host adapter. Pass geometry, material, light and instance data into the shared renderer through the existing value types. A future Hydra delegate should be able to construct those same values without running a ROP or importing SOHO. Keep the out-of-process renderer and its session ownership. [Architecture and ownership](../dev/renderer-architecture.md)

Apply the reuse rule to Phase 2 proxies. Keep ordinary `.bgeo.sc` on Houdini packed disk primitives and handle native packed Alembic in the adapter. The new serialized primitive represents a Blender reference. It and the `s@instance` shortcut share the existing evaluator and worker pool. Store the reference, source frame and last-known bounds with their identity. Load, viewport and bounds callbacks must never launch Blender. Resolve missing or stale bounds asynchronously and display a placeholder box in the meantime.
