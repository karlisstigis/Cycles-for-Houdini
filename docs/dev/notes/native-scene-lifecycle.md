# Scene changes and callback lifetimes

Opening a HIP file replaces the ROP and source nodes while a Cycles Viewer can
remain open. Scene observers now stop observing when their ROP is deleted.
They use the saved node session identity before processing a notification;
comparing a deleted HOM node is itself an exception.

The reported failure was reproduced with the adaptive showcase followed by
the saved `Test.hiplc`. One stale panel observer received later source deletion
notifications and opened 273 **Python Callback Error** dialogs. Closing the
registry's observer alone did not close the panel's independent observer.

Both observers now detach on ROP deletion. `close()` marks the observer inactive
and removes its subscription list before removing host callbacks, so late or
reentrant notifications are inert. Selection/material polling also skips closed
observers. Deleting an ordinary source retains observation of the live ROP and
its source recreation. This does not cook geometry from a callback.

Houdini file-event callbacks also supply keyword metadata such as
`old_hip_file`. The interactive registry and final-render lifecycle handlers
accept that context. Callback removal tolerates a callback already removed by
the host, and the Python Panel tolerates a deleted node-path context.

## Verification

The source-audited evidence (`artifacts/scene-load-callback-recovery-evidence-20261020.json`)
selects ten passing runs on the same compiled runtime. Tests-only restaging is
checked against the binaries, kernels, host runtime and engine identity.

- Both showcase files load, render and replace the scene in one Cycles Viewer
  on the 4090 and 3080 Ti separately, with no callback dialogs or interface
  script errors, unchanged saved files, zero tracked render allocations on
  close, and successful Houdini process exit.
- Twenty-three CPU observer checks cover real editor/multiparm notifications,
  ordinary source recreation, two observers, ROP deletion, late callbacks and
  repeated close without additional geometry cooks.
- Native IPR world updates and eight final-sequence cases retain their
  ownership and cleanup contracts; the final-sequence fixture writes sixteen
  images.
- Fifty CPU transaction checks distinguish strict ownership/configuration/API
  errors from locally recoverable material, texture and world sources.
  Each viewer separately passes thirteen live repair, Pause, Stop and restart
  states on 4090 OptiX. Half-float interactive images use an appropriate color
  tolerance; full-precision output remains checked more strictly.
- Current native texture tests and batch Auto OCIO tests cover missing-image
  diagnostic output, repair, output metadata and retained resources. The saved
  fixture explicitly renders its UV-bearing output with the assigned material.

Earlier failed probes and test-harness corrections remain in the archive.
These checks do not certify general latency, prolonged physical-memory
behavior, all device combinations or release readiness.

The installed package references a versioned runtime. Restart Houdini through
`scripts/launch-sop-showcase.ps1` to use the fix; a running process keeps its
previous runtime.
