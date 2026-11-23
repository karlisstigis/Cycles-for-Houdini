# Stalled native display peer regression

This is an opt-in Houdini GUI test. It starts one isolated Houdini process, one private
loopback TCP listener, and an independent watchdog. It never starts or touches the
artist's MPlay. The child uses a worker-backed `cycles_houdini.Session` and measures
the actual UI-thread calls. The `registry-disconnect` case inserts that same session
into a production `registry.Entry` and invokes the real `registry.poll()` path.

Run from the repository root in PowerShell. Supply the build directory whose
`build-manifest.json` matches the selected package directory. Each trial path must
be new; output is written under ignored `artifacts/`.

```powershell
$common = @{
    BuildDirectory = 'build/windows-h22.0.466-release'
    PackageDirectory = '<matching Houdini package directory>'
    Debugger = 'C:/Program Files (x86)/Windows Kits/10/Debuggers/x64/cdb.exe'
}
& tests/display_stalled_peer/run_trial.ps1 @common -Case resume-layout
& tests/display_stalled_peer/run_trial.ps1 @common -Case disconnect-layout
& tests/display_stalled_peer/run_trial.ps1 @common -Case registry-disconnect
& tests/display_stalled_peer/run_trial.ps1 @common -Case resume-close
& tests/display_stalled_peer/run_trial.ps1 @common -Case resume-replace
& tests/display_stalled_peer/run_trial.ps1 @common -Case resume-coalesce
& tests/display_stalled_peer/run_trial.ps1 @common -Case registry-rop-disconnect
```

`--debugger` is optional to the Python driver, but the trial is inconclusive
without a CDB stack that confirms `UT_NetSocket::write` in the old writer thread.
The watchdog releases or disconnects the peer after three seconds. This is an
imposed held interval, not a claim about the native socket timeout.

Pass requires every measured `present_native()` call, and every timed explicit
close/replace call, to finish strictly below one second. For held calls it also
requires at least two UI heartbeats before release, with gaps below one second.
The peer checks the full byte count and SHA-256 of every plane against the
current frame, including a unique AOV name. Layout recovery must happen in the
same session without a manual display reopen. Coalescing must omit intermediate
AOV layouts on the wire. The registry case first verifies the complete retained
frame after disconnect **before any new apply**, then verifies a newly rendered
same-layout seed edit. Its receipt records the loaded native DSO path and hash,
runtime identity, call timings, peer actions, connection count, and owned cleanup.

Explicit close is a deliberate stop, so that case explicitly reopens the display.
`registry-disconnect` instead tests a transport error. Remote viewer Stop remains
the registry's fatal path; this fixture does not synthesize that control event.

`registry-rop-disconnect` is a supplemental integration case. It creates an actual
Cycles ROP, acquires its entry through `registry.for_rop`, and binds the image port
through `registry.bind_native`. The driver opens an independent authenticated
loopback control connection using the returned token and channel. After image
socket loss, the case requires that the ROP, entry, session, controller, channel,
helper PID, and image port retain their identity while the retained frame and a
new same-layout frame arrive. It then closes the control connection and requires
the normal registry stop path to retire the entry and close the session.
