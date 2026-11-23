# SPDX-License-Identifier: Apache-2.0
"""Opt-in stalled display trial inside an isolated Houdini GUI."""

import hashlib
import json
import os
import threading
import time
import traceback
from pathlib import Path
import hou
import cycles_houdini as ch
from cycles_houdini.device_selection import set_devices as set_rop_devices
from cycles_houdini.ipr import registry
from cycles_houdini.ipr.queued import WorkerSession
from cycles_houdini.scene import native_scene

D = Path(os.environ["CH_PEER_TRIAL"])
CASE = os.environ["CH_PEER_CASE"]
PORT = int(os.environ["CH_PEER_PORT"])
SESSION = None
ENTRY = None
ROP = None
CONTROLLER = None
CHANNEL = None
CONTROL_PID = None
stop_progress_state = None
ROP_CASE = CASE == "registry-rop-disconnect"
state = "init"
deadline = lastbeat = lastpoll = 0.0
AOV = "Peer_" + CASE.replace("-", "_") + "_" + str(os.getpid())
INITIAL_AOV = AOV if CASE in ("registry-disconnect", "registry-rop-disconnect") else "Peer_initial_" + str(os.getpid())
original_present = WorkerSession.present_native


def event(kind, **data):
    with (D / "child-events.jsonl").open("a", encoding="utf8") as f:
        f.write(json.dumps(dict(kind=kind, ns=time.perf_counter_ns(), pid=os.getpid(), **data)) + "\n")


def timed(label, fn):
    image, generation, sequence = SESSION._controller.frame() if SESSION else (None, None, None)
    current = image is not None and SESSION._controller.frame_is_current(image) if SESSION else False
    event(
        "call-enter",
        label=label,
        frame_current=current,
        controller_generation=generation,
        controller_sequence=sequence,
        session_marker=id(SESSION),
        ui_main_thread=threading.current_thread() is threading.main_thread(),
        peer_state="held" if (D / "hold.ack").exists() and (not (D / "released").exists()) else "reading",
    )
    start = time.perf_counter_ns()
    try:
        value, error = (fn(), None)
    except BaseException as exc:
        value, error = (None, repr(exc))
    end = time.perf_counter_ns()
    event(
        "call-return",
        label=label,
        start_ns=start,
        end_ns=end,
        seconds=(end - start) / 1000000000.0,
        error=error,
        healthy=error is None,
        strict_under_1s=end - start < 1000000000,
    )
    return (value, error)


def traced_present(self):
    if self is SESSION:
        image, generation, sequence = self._controller.frame()
        event(
            "call-enter",
            label="registry-present",
            frame_current=image is not None and self._controller.frame_is_current(image),
            controller_generation=generation,
            controller_sequence=sequence,
            session_marker=id(self),
            ui_main_thread=threading.current_thread() is threading.main_thread(),
            peer_state="disconnected" if (D / "disconnect.ack").exists() else "reading",
        )
        start = time.perf_counter_ns()
        error = None
        try:
            return original_present(self)
        except BaseException as exc:
            error = repr(exc)
            raise
        finally:
            end = time.perf_counter_ns()
            event(
                "call-return",
                label="registry-present",
                start_ns=start,
                end_ns=end,
                seconds=(end - start) / 1000000000.0,
                error=error,
                healthy=error is None,
                strict_under_1s=end - start < 1000000000,
            )
    return original_present(self)


def expected():
    frame = SESSION.frame()
    info = SESSION.frame_info()
    assert frame and AOV in frame["planes"]
    data = dict(
        session_marker=id(SESSION),
        generation=info["generation"],
        sequence=info["sequence"],
        aov=AOV,
        width=frame["width"],
        height=frame["height"],
        planes={
            n: dict(channels=p["channels"], bytes=len(p["pixels"]), sha256=hashlib.sha256(p["pixels"]).hexdigest())
            for n, p in frame["planes"].items()
        },
    )
    (D / "expected.json.tmp").write_text(json.dumps(data))
    (D / "expected.json.tmp").replace(D / "expected.json")
    event(
        "expected",
        session_marker=data["session_marker"],
        generation=data["generation"],
        sequence=data["sequence"],
        aov=AOV,
    )


def prime_frame():
    SESSION.wait(SESSION.apply({"aovs": [{"name": INITIAL_AOV, "type": "value"}]}), timeout=60)
    timed("normal-present", SESSION.present_native)
    ch._native._test_wait_display(SESSION._handle)
    SESSION.wait(SESSION.apply({"rendering": {"seed": 77}}), timeout=60)


def finish(result):
    global state
    state = "finished"
    event(
        "result",
        result=result,
        session_marker=id(SESSION) if SESSION else None,
        binding=ENTRY.socket_port if ENTRY else None,
    )
    if ENTRY and not ROP_CASE:
        registry._entries.pop(ENTRY.key, None)
    WorkerSession.present_native = original_present
    if ROP_CASE:
        registry.close_all()
    elif SESSION:
        timed("cleanup-close", SESSION.close_native_display)
        SESSION.close()
    event("session-closed")
    hou.ui.removeEventLoopCallback(tick)
    hou.exit(0 if result == "measured" else 1, suppress_save_prompt=True)


def tick():
    global \
        SESSION, \
        ENTRY, \
        ROP, \
        CONTROLLER, \
        CHANNEL, \
        CONTROL_PID, \
        stop_progress_state, \
        state, \
        deadline, \
        lastbeat, \
        lastpoll
    now = time.perf_counter()
    if now - lastbeat > 0.025:
        event("heartbeat", ui_thread=threading.get_ident())
        lastbeat = now
    try:
        if state == "init":
            assert hou.isUIAvailable() and threading.current_thread() is threading.main_thread()
            native = Path(ch._native.__file__).resolve()
            event(
                "init",
                ui_available=True,
                main_thread=threading.get_ident(),
                native=str(native),
                native_sha256=hashlib.sha256(native.read_bytes()).hexdigest(),
                case=CASE,
                execution="worker",
            )
            if ROP_CASE:
                event("rop-setup", stage="create")
                ROP = hou.node("/out").createNode("cycles_ipr", "peer_rop")
                set_rop_devices(ROP, "CPU")
                for name, value in dict(
                    resx=2048,
                    resy=1024,
                    samples=1,
                    ipr_samples=1,
                    render_threads=4,
                    resolution_percent=100,
                    ipr_resolution_percent=100,
                ).items():
                    ROP.parm(name).set(value)
                event("rop-setup", stage="configuration")
                config = native_scene.configuration(ROP)
                assert config[1:4] == (2048, 1024, 1), config
                event("rop-setup", stage="for_rop", configuration=repr(config))
                ENTRY = registry.for_rop(ROP, config)
                SESSION = ENTRY.session
                assert isinstance(SESSION, WorkerSession)
                CONTROLLER = SESSION._controller
                event("rop-setup", stage="calibration-start")
                SESSION.wait(SESSION.calibration(), timeout=60)
                event("rop-setup", stage="calibration-complete")
                WorkerSession.present_native = traced_present
                event("rop-setup", stage="bind-native")
                binding, error = timed("normal-bind-native", lambda: registry.bind_native(ENTRY, str(PORT)))
                assert error is None, error
                CHANNEL = binding["channel"]
                (D / "control-bind.json.tmp").write_text(json.dumps(binding))
                (D / "control-bind.json.tmp").replace(D / "control-bind.json")
                event(
                    "rop-bound",
                    rop_id=ROP.sessionId(),
                    rop_path=ROP.path(),
                    entry_key=ENTRY.key,
                    entry_marker=id(ENTRY),
                    session_marker=id(SESSION),
                    controller_marker=id(CONTROLLER),
                    channel=CHANNEL,
                    socket_port=ENTRY.socket_port,
                )
                state, deadline = "await-control", now + 10
                return
            SESSION = ch.Session(
                device="CPU",
                width=2048,
                height=1024,
                samples=1,
                interactive=False,
                threads=4,
                passes=["combined"],
                execution="worker",
            )
            SESSION.wait(SESSION.calibration(), timeout=60)
            timed("normal-open", lambda: SESSION.open_native_display("ip", PORT))
            prime_frame()
            if CASE == "registry-disconnect":
                key = "peer-regression:" + str(os.getpid())
                ENTRY = registry.Entry(key, ("CPU", 2048, 1024, 1), SESSION, socket_port=str(PORT))
                registry._entries[key] = ENTRY
                registry._last_texture_poll = time.monotonic()
                WorkerSession.present_native = traced_present
            (D / "hold.request").touch()
            state, deadline = ("await-hold", now + 10)
        elif state == "await-control":
            registry._last_display = 0.0
            registry.poll()
            connected = D / "control-connected.json"
            if connected.exists():
                connection = json.loads(connected.read_text())
                if ENTRY.helper_pid == connection["pid"] and ENTRY.channel == connection["channel"] == CHANNEL:
                    CONTROL_PID = connection["pid"]
                    event("control-authenticated", channel=CHANNEL, helper_pid=CONTROL_PID)
                    prime_frame()
                    (D / "hold.request").touch()
                    state, deadline = "await-hold", time.perf_counter() + 10
            if now > deadline:
                finish("failed-control-handshake")
        elif state == "await-hold" and (D / "hold.ack").exists():
            timed("held-present-enqueue", SESSION.present_native)
            event("await-writer-io")
            state, deadline = ("await-io", now + 30)
        elif state == "await-io" and (D / "io-confirmed.json").exists():
            proof = json.loads((D / "io-confirmed.json").read_text())
            event("writer-io-evidence", **proof)
            if not proof["confirmed"]:
                finish("inconclusive-writer-io")
                return
            if CASE in ("registry-disconnect", "registry-rop-disconnect"):
                (D / "disconnect.request").touch()
                state, deadline = ("await-disconnect", now + 12)
                return
            SESSION.wait(SESSION.apply({"aovs": [{"name": AOV + "a", "type": "value"}]}), timeout=60)
            if CASE == "resume-coalesce":
                timed("coalesce-present-1", SESSION.present_native)
                SESSION.wait(SESSION.apply({"aovs": [{"name": AOV + "b", "type": "value"}]}), timeout=60)
                timed("coalesce-present-2", SESSION.present_native)
            SESSION.wait(SESSION.apply({"aovs": [{"name": AOV, "type": "value"}]}), timeout=60)
            expected()
            if CASE in ("resume-layout", "disconnect-layout", "resume-coalesce"):
                timed("held-layout-present", SESSION.present_native)
            elif CASE == "resume-close":
                timed("held-explicit-close", SESSION.close_native_display)
                event("manual-reopen-required", reason="explicit-close-requested")
                timed("reopen-after-explicit-close", lambda: SESSION.open_native_display("ip", PORT))
                timed("post-reopen-present", SESSION.present_native)
            elif CASE == "resume-replace":
                timed("held-display-replace", lambda: SESSION.open_native_display("ip", PORT))
                timed("replacement-present", SESSION.present_native)
            state, deadline = ("await-frame", time.perf_counter() + 12)
        elif state == "await-disconnect" and (D / "disconnect.ack").exists():
            expected()
            state, deadline = ("await-retained-frame", now + 12)
        elif state == "await-retained-frame":
            if (D / "received.json").exists():
                (D / "received.json").replace(D / "received-retained.json")
                event("retained-frame-verified")
                (D / "expected.json").unlink(missing_ok=True)
                SESSION.wait(SESSION.apply({"rendering": {"seed": 78}}), timeout=60)
                (D / "received.json").unlink(missing_ok=True)
                expected()
                state, deadline = ("await-frame", now + 12)
                return
            if now - lastpoll > 0.05:
                lastpoll = now
                registry._last_display = 0.0
                registry.poll()
                if ENTRY.socket_port == "":
                    event("registry-binding-cleared", error=ENTRY.error, state=SESSION.status()["state"])
                    finish("failed-registry-binding-cleared")
                    return
            if now > deadline:
                finish("failed-retained-frame")
        elif state == "await-frame":
            if (D / "received.json").exists():
                if ROP_CASE:
                    assert registry.get(ENTRY.key) is ENTRY
                    assert ENTRY.rop is ROP and ROP.sessionId() == int(ENTRY.key.split(":")[1])
                    assert ENTRY.session is SESSION and SESSION._controller is CONTROLLER
                    assert ENTRY.channel == CHANNEL and ENTRY.helper_pid == CONTROL_PID
                    assert ENTRY.socket_port == str(PORT)
                    assert SESSION.status()["state"] not in ("closed", "cancelled")
                    event(
                        "rop-survived",
                        rop_id=ROP.sessionId(),
                        entry_marker=id(ENTRY),
                        session_marker=id(SESSION),
                        controller_marker=id(CONTROLLER),
                        channel=ENTRY.channel,
                        helper_pid=ENTRY.helper_pid,
                        socket_port=ENTRY.socket_port,
                    )
                    (D / "control-stop.request").touch()
                    state, deadline = "await-control-stop", now + 20
                    return
                finish("measured")
                return
            if now - lastpoll > 0.05:
                lastpoll = now
                if CASE in ("registry-disconnect", "registry-rop-disconnect"):
                    registry._last_display = 0.0
                    registry.poll()
                    if ENTRY.socket_port == "":
                        event("registry-binding-cleared", error=ENTRY.error, state=SESSION.status()["state"])
                        finish("failed-registry-binding-cleared")
                        return
                else:
                    _, error = timed("post-call-present-poll", SESSION.present_native)
                    if error:
                        event("post-call-error", error=error)
            if now > deadline:
                finish("failed-no-verified-frame")
        elif state == "await-control-stop":
            if (D / "control-stop.ack").exists():
                registry._last_display = 0.0
                registry.poll()
                status = SESSION.status()["state"]
                retired = registry.get(ENTRY.key) is None
                if status != stop_progress_state:
                    stop_progress_state = status
                    event(
                        "control-stop-progress",
                        session_state=status,
                        entry_retired=retired,
                        channel=ENTRY.channel,
                        socket_port=ENTRY.socket_port,
                    )
                if retired and ENTRY.channel == "" and ENTRY.socket_port == "" and status == "closed":
                    event("control-stop-verified", rop_id=ROP.sessionId(), session_marker=id(SESSION))
                    finish("measured")
                    return
            if now > deadline:
                event(
                    "control-stop-timeout",
                    session_state=SESSION.status()["state"],
                    entry_present=registry.get(ENTRY.key) is not None,
                    channel=ENTRY.channel,
                    socket_port=ENTRY.socket_port,
                )
                finish("failed-control-stop")
        elif state != "finished" and now > deadline:
            finish("failed-stage-timeout-" + state)
    except Exception:
        event("exception", error=traceback.format_exc())
        try:
            finish("failed-exception")
        except Exception:
            event("cleanup-exception", error=traceback.format_exc())
            hou.exit(1, suppress_save_prompt=True)


hou.ui.addEventLoopCallback(tick)
