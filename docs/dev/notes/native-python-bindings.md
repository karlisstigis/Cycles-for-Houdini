# Native Python bindings

`src/houdini/python_module.cpp` contains the single `_cycles_houdini` pybind11
initializer and the public registration signatures. Implementation lives in four
translation units:

| Source | Responsibility |
| --- | --- |
| `python_scene.cpp` | Host capture, prepared transactions, frozen geometry and material conversion |
| `python_snapshot.cpp` | Owned snapshots, navigation rebasing, worker configuration and reply conversion |
| `python_frame.cpp` | Frame ownership, readonly buffers, display conversion and output |
| `python_session.cpp` | Session lifecycle, input conversion, status and host utility bindings |

`python_binding_detail.h` is private to these bindings. It declares shared
helpers through the public renderer types and forward declarations; it has
no concrete Houdini SDK includes. `python_binding_state.h` holds the shared
session handle and capture guard for implementation files. Capture leases,
prepared transactions and navigation implementation types stay in their
owning translation units. SDK, adapter and fixture includes belong to the
implementations that use them.
`python_bindings.cpp` retains the existing exception translators and status
registration; `python_status.cpp` retains the pure status conversion contracts.

The session capsule owns its `Handle`. A prepared transaction retains its
Python capsule owner. Frame capsules hold a shared immutable `FrameEvent`.
Each exported readonly `FramePixels` buffer holds another shared owner, so a
consumer can outlive session closure, its frame capsule and the worker reply
mapping. Final native owner destruction releases the GIL through the existing
pybind11 RAII scope. Extraction does not change these ownership rules or the
public argument/default/exception contracts.

## Handle lifetime and binding lock order

`Handle::state_mutex` protects admission of scene transactions, close and
capture retirement, display ownership, and readers of binding-owned capture
state. An admitted transaction keeps `applying` set through conversion and
publication. Close rejects during a transaction. Capture retirement rejects
during a transaction and publishes its retired state before releasing frozen
resources. Status uses pre-transaction extraction and point-instance counters
while conversion owns the adapter. Object keys and material references read
committed state; material paths and stylesheet resources use the last accepted
view while a conversion is active. COP readers pin the committed batch and
copy its counters under the binding mutex. Frame and snapshot capsules own
their data independently of close.

The binding lock protects publication of the native display. A presenter
pins a `shared_ptr<TileDisplay>` under the lock and releases the lock and
the GIL before presenting. A replacement claims the destination before the
handle becomes visible to other presenters. The ownership token prevents a
presenter pinned to the old display from writing to its replacement.

Each destination serializes its HDK writers. A layout change, image-socket
close or display replacement requests retirement and returns to the caller.
A background thread joins the old writer before activating the latest
pending layout and frame for that destination. Other destinations can
continue writing. An explicit file-device close waits for finalization so
callers can immediately read the completed file.

IPR retries a failed image connection on the same port while the control
channel remains bound. It re-presents the current retained frame and keeps
the render session. Explicit viewer Stop and control-channel closure still
follow the registry's existing cancellation and ownership rules. Process
shutdown drains outstanding display writers while the HDK is loaded. A peer
that permanently stops reading can delay that final drain.

Close marks the handle as closing under the binding lock, then releases both
that lock and the GIL before display destruction and the core session close.
A concurrent close waits without the GIL until the first close finishes. A
same-thread callback attempting to reenter close raises a logic error rather
than waiting on itself. On failure, the closing flag is cleared and waiters
can retry; success retains the `RenderSession` pointer so post-close status
continues to report the core's `Closed` state. Cancellation takes the session's control lock without the binding lock, so it
can interrupt a render or wait. A policy callback takes its independent
`PolicySlot` mutex to copy an immutable policy owner, then resolves outside it.

## Color configuration and lock order

Release the binding lock before acquiring the core session control mutex.
Session state changes take the control mutex before the frame-store mutex.
A worker joins its own render before replacing its OCIO configuration. Frames
already delivered retain their source configuration and pixel storage.

Explicit sync sessions in the same process share stock Cycles color state.
A separate admission mutex protects the small table of live sync identities.
The mutex is released before renderer work. Closing a session releases its
admission after teardown. Conflicting overlapping sync policies produce a
diagnostic directing the caller to worker execution. [Color configuration](native-color.md)

## Lifetime regression probes

`tests/houdini_native_lifetime_contract.py` provides CPU cases, each run
in a fresh bounded hython process with isolated preferences and reports:

- `frame-close`: acquire a real frame's readonly view before closing the
  session; verify its hash after closure and capsule deletion, allocate a
  replacement render, and release the last consumer on another thread.
- `concurrent-close`: observe the native EXR writer's unique staged file
  while the GIL-releasing write call is active, close the same session,
  require completion and zero tracked allocations, and render again.
- `worker-mapping`: hold the actual named worker reply mapping and a decoded
  frame, terminate only the test-owned worker before its ACK, verify and
  decode the mapping after exit, release it, verify retained frame bytes,
  then start and close a fresh worker successfully.
- `display-close` and `display-replace`: a test-only rendezvous pauses a
  presenter after it pins the production display owner. A second thread
  detaches or replaces the display, then the presenter resumes. The wait is
  bounded and does not add timing sleeps to production presentation.
- `display-ordering`: hold an old-layout presenter while the same session
  renders a newer AOV layout, then verify every native EXR subimage after
  releasing the old presenter. Also check caller serials across source
  generations, invalid-dimension rejection, stale recolor rejection, and
  equal-serial recolor against independent OCIO transforms. A bounded writer
  rendezvous returns completed-write counts; no GUI or timing sleeps are used.
- `transaction-close`: a prepared scene transaction prevents close until
  discarded; repeated close and replacement-session creation then succeed.
- `capture-retire-reader`: a test-only rendezvous pauses admitted capture
  retirement. Status, adapter, material and COP readers inspect the handle
  before the release; afterward the test verifies post-retirement status and
  repeated close. The accepted path/resource views and frozen COP ownership
  are released on retirement and close.

Run the eight CPU selectors through
`tests/pytest_cases/test_native_lifetime.py` under the verified native hython
environment. For the focused GPU lifecycle, run
`tests/native_test_entry.py tests/houdini_native_lifetime_contract.py --case display-replace --device <explicit OptiX device ID> --report <private report path>`
with the same environment. `frame-close` can be selected with that device ID
to verify retained GPU frame bytes after close. The test subprocesses have
bounded waits and do not alter the installed runtime.

The original implementation passed all three before extraction on the
qualified Houdini 22.0.466 build with manifest SHA-256
`fa92308adbd38eddad27f00822efc19e44f321191bd82e8748da5aa1b6df45f7`.
The private baseline receipts are under
`artifacts/stage3b/baseline-20261109/`, including the copied manifest and actual
loaded-runtime identity for each child. No original-implementation failure
was observed. Repeat these cases and the existing pybind front, argument
buffer and snapshot ownership contracts after extraction.
