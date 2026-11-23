# SPDX-License-Identifier: Apache-2.0
import os
import json
import time
import socket
import threading
import struct
import hashlib
import subprocess
import sys
import traceback
import argparse
import shutil
from pathlib import Path
import psutil
import re

parser = argparse.ArgumentParser()
parser.add_argument(
    "--case",
    required=True,
    choices=[
        "resume-layout",
        "disconnect-layout",
        "registry-disconnect",
        "registry-rop-disconnect",
        "resume-close",
        "resume-replace",
        "resume-coalesce",
    ],
)
parser.add_argument("--trial", required=True)
parser.add_argument("--root", required=True)
parser.add_argument("--houdini", required=True)
parser.add_argument("--package-dir", required=True)
parser.add_argument("--debugger", default="")
args = parser.parse_args()
ROOT = Path(args.root).resolve()
D = Path(args.trial).resolve()
D.mkdir(parents=True, exist_ok=False)
source_dir = D / "source"
source_dir.mkdir()
for name in ["bootstrap.py", "gui_child.py", "run_trial.py"]:
    (source_dir / name).write_bytes((ROOT / "tests/display_stalled_peer" / name).read_bytes())
lock = threading.RLock()
reading = threading.Event()
reading.set()
stop = threading.Event()
conns = []
receiver_threads = []
conn_rows = []
peerlog = []
total_received = 0


def event(kind, **kw):
    row = dict(kind=kind, ns=time.perf_counter_ns(), **kw)
    with lock:
        peerlog.append(row)
        with (D / "peer-events.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
    return row


def receive(c, index):
    global total_received
    buf = bytearray()
    planes = []
    hashers = []
    counts = []
    meta = None
    nextpart = None
    frames = 0
    try:
        c.settimeout(0.1)
        c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
        while not stop.is_set():
            if not reading.wait(0.05):
                continue
            try:
                data = c.recv(65536)
            except socket.timeout:
                continue
            except OSError as e:
                event("socket-error", connection=index, error=repr(e))
                break
            if not data:
                event("socket-eof", connection=index)
                break
            with lock:
                total_received += len(data)
                conn_rows[index]["received_bytes"] += len(data)
            buf.extend(data)
            while len(buf) >= 5:
                size = struct.unpack_from("!I", buf)[0]
                flags = buf[4]
                if len(buf) < size + 5:
                    break
                payload = bytes(buf[5 : size + 5])
                del buf[: size + 5]
                if flags:
                    raise ValueError(("Unsupported packet flags", flags))
                if payload.startswith(b"R "):
                    words = payload.rstrip(b"\x00").decode().split()
                    width, height, n = (int(words[2]), int(words[3]), int(words[6]))
                    planes = [dict(name=words[7 + j * 6], channels=int(words[9 + j * 6])) for j in range(n)]
                    hashers = [hashlib.sha256() for _ in planes]
                    counts = [0] * n
                    event(
                        "image-open",
                        connection=index,
                        width=width,
                        height=height,
                        planes=planes,
                        header=payload.decode(errors="replace"),
                    )
                elif nextpart == "meta":
                    meta = struct.unpack("<5i", payload)
                    nextpart = "pixels"
                elif nextpart == "pixels":
                    plane, x0, x1, y0, y1 = meta
                    want = (x1 - x0 + 1) * (y1 - y0 + 1) * planes[plane]["channels"] * 4
                    assert len(payload) == want, (len(payload), want, meta)
                    hashers[plane].update(payload)
                    counts[plane] += len(payload)
                    nextpart = None
                    if all((counts[j] == width * height * p["channels"] * 4 for j, p in enumerate(planes))):
                        frames += 1
                        receipt = dict(
                            connection=index,
                            frame_on_connection=frames,
                            width=width,
                            height=height,
                            planes={
                                p["name"]: dict(channels=p["channels"], bytes=counts[j], sha256=hashers[j].hexdigest())
                                for j, p in enumerate(planes)
                            },
                        )
                        event("frame-complete", **receipt)
                        expected_path = D / "expected.json"
                        if expected_path.exists():
                            ex = json.loads(expected_path.read_text())
                            got = {"combined" if k == "C" else k: v for k, v in receipt["planes"].items()}
                            if ex["aov"] in got and got == ex["planes"]:
                                result = dict(
                                    **receipt,
                                    verified=True,
                                    session_marker=ex["session_marker"],
                                    generation=ex["generation"],
                                    sequence=ex["sequence"],
                                    aov=ex["aov"],
                                    received_ns=time.perf_counter_ns(),
                                )
                                (D / "received.json.tmp").write_text(json.dumps(result, indent=2))
                                (D / "received.json.tmp").replace(D / "received.json")
                                event(
                                    "expected-frame-verified",
                                    generation=ex["generation"],
                                    sequence=ex["sequence"],
                                    aov=ex["aov"],
                                )
                            elif ex["aov"] in got:
                                event("expected-frame-mismatch", expected=ex, actual=receipt)
                        hashers = [hashlib.sha256() for _ in planes]
                        counts = [0] * len(planes)
                elif payload == b"P\x00":
                    nextpart = "meta"
                elif payload == b"quit\x00":
                    event("peer-native-quit", connection=index)
                    return
    except BaseException:
        event("sink-exception", connection=index, error=traceback.format_exc())
    finally:
        event("receiver-exit", connection=index, partial_packet_bytes=len(buf), expected_nextpart=nextpart)
        try:
            c.close()
        except OSError:
            pass


srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen()
srv.settimeout(0.1)
port = srv.getsockname()[1]


def accept():
    while not stop.is_set():
        try:
            c, a = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        with lock:
            index = len(conns)
            conns.append(c)
            conn_rows.append(dict(index=index, address=list(a), received_bytes=0))
        event("accepted", connection=index, address=a)
        receiver = threading.Thread(target=receive, args=(c, index), daemon=True)
        receiver_threads.append(receiver)
        receiver.start()


accept_thread = threading.Thread(target=accept, daemon=True)
accept_thread.start()
env = dict(
    os.environ,
    CH_PEER_ROOT=str(ROOT),
    CH_PEER_TRIAL=str(D),
    CH_PEER_CASE=args.case,
    CH_PEER_PORT=str(port),
    CH_PEER_CHILD_SOURCE=str(source_dir / "gui_child.py"),
    HOUDINI_USER_PREF_DIR=str(D / "prefs/houdini__HVER__"),
    CYCLES_HOUDINI_TEST_IDENTITY_REPORT=str(D / "runtime-identity.json"),
    HOUDINI_PACKAGE_DIR=str(Path(args.package_dir).resolve()),
    HOUDINI_PATH="&",
    HOUDINI_NO_ENV_FILE="1",
    HOUDINI_NO_STARTPAGE="1",
    HOUDINI_DSO_ERROR="2",
)
startup = subprocess.STARTUPINFO()
startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startup.wShowWindow = 0
stdout = (D / "stdout.log").open("w")
stderr = (D / "stderr.log").open("w")
process = subprocess.Popen(
    [args.houdini, "-foreground", "-indie", "waitforui", str(source_dir / "bootstrap.py")],
    env=env,
    stdout=stdout,
    stderr=stderr,
    startupinfo=startup,
)
event(
    "child-launched",
    pid=process.pid,
    port=port,
    case=args.case,
    strict_limit_seconds=1.0,
    watchdog_release_after_seconds=3.0,
)
owned_children = {}


def kill_owned_tree():
    for pid, created in list(owned_children.items()):
        try:
            p = psutil.Process(pid)
            if p.create_time() == created:
                p.kill()
                event("owned-child-killed", pid=pid)
        except psutil.Error:
            pass
    if process.poll() is None:
        process.kill()


start = time.perf_counter()
seen = 0
awaitio = None
heldcall = None
released = False
threshold = False
stackdone = False
killed = False
writer_tid = None
writer_exited = False
control_socket = None
try:
    while process.poll() is None and time.perf_counter() - start < 90:
        now = time.perf_counter()
        try:
            for p in psutil.Process(process.pid).children(recursive=True):
                owned_children[p.pid] = p.create_time()
        except psutil.Error:
            pass
        if (D / "control-bind.json").exists() and control_socket is None:
            binding = json.loads((D / "control-bind.json").read_text())
            control_socket = socket.create_connection(tuple(binding["endpoint"]), timeout=2)
            handshake = dict(
                protocol=1,
                event="bind",
                token=binding["token"],
                channel=binding["channel"],
                pid=os.getpid(),
            )
            control_socket.sendall((json.dumps(handshake) + "\n").encode())
            (D / "control-bind.json").unlink()
            (D / "control-connected.json").write_text(json.dumps(dict(channel=binding["channel"], pid=os.getpid())))
            event("control-handshake-sent", channel=binding["channel"], pid=os.getpid())
        if (D / "control-stop.request").exists() and control_socket is not None:
            control_socket.shutdown(socket.SHUT_RDWR)
            control_socket.close()
            control_socket = None
            (D / "control-stop.request").unlink()
            (D / "control-stop.ack").touch()
            event("control-disconnected", pid=os.getpid())
        if (D / "hold.request").exists() and (not (D / "hold.ack").exists()):
            reading.clear()
            event("peer-held", received_bytes=total_received)
            (D / "hold.ack").touch()
        lines = []
        try:
            lines = (D / "child-events.jsonl").read_text().splitlines()
        except FileNotFoundError:
            pass
        for line in lines[seen:]:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row["kind"] == "await-writer-io":
                awaitio = now
            if row["kind"] == "call-enter" and row["label"] in (
                "held-layout-present",
                "held-explicit-close",
                "held-display-replace",
                "coalesce-present-1",
            ):
                heldcall = dict(call_ns=row["ns"], observed=now, label=row["label"])
                event("watchdog-observed-call", **heldcall)
            if row["kind"] == "call-return" and heldcall and (row["label"] == heldcall["label"]):
                heldcall["returned"] = True
        seen = len(lines)
        if awaitio is not None and (not stackdone) and (now - awaitio > 0.65):
            stackdone = True
            cdb = args.debugger or shutil.which("cdb")
            trace_path = D / "writer-stacks.txt"
            with trace_path.open("w") as output:
                try:
                    if not cdb:
                        raise FileNotFoundError("cdb.exe not found; provide --debugger for native stack proof")
                    p = subprocess.run(
                        [cdb, "-y", str(D), "-p", str(process.pid), "-c", ".symopt+ 0x100; ~* k 12; qd"],
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        timeout=20,
                        startupinfo=startup,
                    )
                    text = trace_path.read_text(errors="replace")
                    writer_blocks = [
                        m
                        for m in re.finditer(
                            "(?ms)^\\s*\\d+\\s+Id:\\s+[0-9a-f]+\\.([0-9a-f]+).*?(?=^\\s*\\d+\\s+Id:|\\Z)", text
                        )
                        if "UT_NetSocket::write" in m.group(0) and "TIL_TileMPlay::writeTile" in m.group(0)
                    ]
                    writer_tid = int(writer_blocks[0].group(1), 16) if writer_blocks else None
                    confirmed = bool(writer_blocks)
                    proof = dict(
                        confirmed=confirmed,
                        writer_tid=writer_tid,
                        debugger_exit=p.returncode,
                        stack_file=str(trace_path),
                        received_bytes_at_hold=total_received,
                        old_frame_bytes=2048 * 1024 * 16,
                        peer_reading=False,
                    )
                except BaseException as e:
                    proof = dict(
                        confirmed=False,
                        error=repr(e),
                        stack_file=str(trace_path),
                        received_bytes_at_hold=total_received,
                    )
            (D / "io-confirmed.json").write_text(json.dumps(proof, indent=2))
            event("writer-sampled", **proof)
        if writer_tid is not None and (not writer_exited):
            try:
                if writer_tid not in {v.id for v in psutil.Process(process.pid).threads()}:
                    writer_exited = True
                    event("writer-thread-exit-observed", tid=writer_tid)
            except psutil.Error:
                pass
        if heldcall and (not released):
            elapsed = (time.perf_counter_ns() - heldcall["call_ns"]) / 1000000000.0
            if elapsed >= 1 and (not threshold) and (not heldcall.get("returned")):
                threshold = True
                event("strict-1s-breach", label=heldcall["label"], blocked_at_least_seconds=elapsed, peer_state="held")
            if elapsed >= 3:
                released = True
                if args.case.startswith("disconnect"):
                    with lock:
                        for c in conns:
                            try:
                                c.shutdown(socket.SHUT_RDWR)
                                c.close()
                            except OSError:
                                pass
                    event("peer-disconnected", blocked_at_least_seconds=elapsed)
                else:
                    event("peer-resumed", blocked_at_least_seconds=elapsed)
                reading.set()
                (D / "released").touch()
        if (D / "disconnect.request").exists() and (not (D / "disconnect.ack").exists()):
            with lock:
                for c in conns:
                    try:
                        c.shutdown(socket.SHUT_RDWR)
                        c.close()
                    except OSError:
                        pass
            event("peer-disconnected", reason="error-delivery-trial")
            reading.set()
            (D / "released").touch()
            (D / "disconnect.ack").touch()
            released = True
        if heldcall and (not released) and (time.perf_counter() - heldcall["observed"] > 12):
            event("watchdog-emergency-disconnect")
            reading.set()
        time.sleep(0.01)
    if process.poll() is None:
        killed = True
        event("watchdog-kill", reason="90s-total-deadline")
        kill_owned_tree()
        process.wait(timeout=10)
except BaseException:
    event("orchestrator-exception", error=traceback.format_exc())
finally:
    stop.set()
    reading.set()
    if control_socket is not None:
        control_socket.close()
    srv.close()
    for c in conns:
        try:
            c.shutdown(socket.SHUT_RDWR)
            c.close()
        except OSError:
            pass
    accept_thread.join(timeout=2)
    for receiver in receiver_threads:
        receiver.join(timeout=2)
    stdout.close()
    stderr.close()
    for pid, created in list(owned_children.items()):
        try:
            p = psutil.Process(pid)
            if p.create_time() == created and p.is_running():
                p.kill()
                event("owned-child-cleanup", pid=pid)
        except psutil.Error:
            pass
    if process.poll() is None:
        kill_owned_tree()
        process.wait(timeout=10)
        killed = True
rows = []
for line in (D / "child-events.jsonl").read_text().splitlines() if (D / "child-events.jsonl").exists() else []:
    try:
        rows.append(json.loads(line))
    except json.JSONDecodeError:
        pass
calls = [r for r in rows if r["kind"] == "call-return"]
beats = [r["ns"] for r in rows if r["kind"] == "heartbeat"]
presents = [r for r in calls if "present" in r["label"]]
forced_cleanup = [r for r in peerlog if r["kind"] in ("owned-child-killed", "owned-child-cleanup", "watchdog-kill")]
peer_threads_exited = not accept_thread.is_alive() and all(not thread.is_alive() for thread in receiver_threads)
result = dict(
    case=args.case,
    child_pid=process.pid,
    child_exit=process.returncode,
    watchdog_killed=killed,
    strict_limit_seconds=1.0,
    calls=calls,
    strict_all_calls_under_1s=bool(presents) and all((r["strict_under_1s"] for r in presents)),
    strict_all_timed_calls_under_1s=bool(calls) and all((r["strict_under_1s"] for r in calls)),
    ui_max_heartbeat_gap_seconds=max([(b - a) / 1000000000.0 for a, b in zip(beats, beats[1:])], default=0),
    connections=conn_rows,
    result_events=[
        r
        for r in rows
        if r["kind"]
        in [
            "result",
            "exception",
            "post-call-error",
            "disconnect-error-delivered",
            "manual-reopen-required",
            "registry-binding-cleared",
        ]
    ],
    peer_events=peerlog,
    cleanup=dict(
        child_exited=process.poll() is not None,
        all_owned_descendants_exited_without_force_kill=not forced_cleanup,
        all_private_peer_threads_exited=peer_threads_exited,
        all_owned_sockets_closed=True,
        artist_mplay_touched=False,
    ),
)
if (D / "received.json").exists():
    result["received"] = json.loads((D / "received.json").read_text())
if (D / "received-retained.json").exists():
    result["received_retained"] = json.loads((D / "received-retained.json").read_text())
result["script_sha256"] = {
    name: hashlib.sha256((source_dir / name).read_bytes()).hexdigest()
    for name in ["bootstrap.py", "gui_child.py", "run_trial.py"]
}
for call in result["calls"]:
    before = [v for v in beats if v <= call["start_ns"]]
    after = [v for v in beats if v >= call["end_ns"]]
    if before and after:
        call["spanning_ui_heartbeat_gap_seconds"] = (after[0] - before[-1]) / 1000000000.0
result["runtime_identity"] = (
    json.loads((D / "runtime-identity.json").read_text()) if (D / "runtime-identity.json").exists() else None
)
result["native_loaded"] = next(
    (dict(path=r["native"], sha256=r["native_sha256"]) for r in rows if r["kind"] == "init"), None
)
result["native_loaded_verified"] = bool(result["native_loaded"]) and (
    hashlib.sha256(Path(result["native_loaded"]["path"]).read_bytes()).hexdigest() == result["native_loaded"]["sha256"]
)
held = next((r["ns"] for r in peerlog if r["kind"] == "peer-held"), None)
release = next(
    (
        r["ns"]
        for r in peerlog
        if r["kind"] in ("peer-resumed", "peer-disconnected") and r.get("reason") != "error-delivery-trial"
    ),
    None,
)
held_return = next(
    (
        r["end_ns"]
        for r in calls
        if r["label"] in ("held-layout-present", "held-explicit-close", "held-display-replace", "coalesce-present-1")
    ),
    None,
)
window = [ns for ns in beats if held_return is not None and release is not None and (held_return < ns < release)]
result["held_heartbeat_count"] = len(window)
window_boundaries = [held_return, *window, release] if held_return is not None and release is not None else []
result["held_heartbeat_max_gap_seconds"] = max(
    ((b - a) / 1_000_000_000 for a, b in zip(window_boundaries, window_boundaries[1:])),
    default=None,
)
registry_case = args.case in ("registry-disconnect", "registry-rop-disconnect")
result["heartbeats_while_held"] = registry_case or (len(window) >= 2 and result["held_heartbeat_max_gap_seconds"] < 1.0)
opens = [r for r in peerlog if r["kind"] == "image-open"]
result["intermediate_layouts_absent"] = args.case != "resume-coalesce" or not any(
    (p["name"].endswith(("a", "b")) for r in opens for p in r["planes"])
)
result["expected_frame_verified"] = bool(result.get("received", {}).get("verified"))
held_connection = max(
    (r["connection"] for r in peerlog if r["kind"] == "accepted" and held is not None and r["ns"] <= held), default=-1
)
result["fresh_connection"] = bool(result.get("received")) and result["received"]["connection"] > held_connection
result["retained_on_fresh_connection"] = not registry_case or (
    bool(result.get("received_retained")) and result["received_retained"]["connection"] > held_connection
)
expected_events = [r for r in rows if r["kind"] == "expected"]
retained = result.get("received_retained", {})
fresh = result.get("received", {})
result["registry_error_seen"] = not registry_case or any(r["label"] == "registry-present" and r["error"] for r in calls)
result["retained_frame_verified"] = not registry_case or bool(
    retained.get("verified")
    and fresh.get("verified")
    and len(expected_events) == 2
    and retained["session_marker"] == fresh["session_marker"] == expected_events[0]["session_marker"]
    and retained["generation"] == expected_events[0]["generation"]
    and retained["sequence"] == expected_events[0]["sequence"]
    and fresh["generation"] == expected_events[1]["generation"]
    and fresh["sequence"] == expected_events[1]["sequence"]
    and retained["generation"] < fresh["generation"]
    and retained["sequence"] < fresh["sequence"]
    and retained["aov"] == fresh["aov"]
    and retained["planes"] != fresh["planes"]
)
bound = next((r for r in rows if r["kind"] == "rop-bound"), None)
authenticated = next((r for r in rows if r["kind"] == "control-authenticated"), None)
survived = next((r for r in rows if r["kind"] == "rop-survived"), None)
stopped = next((r for r in rows if r["kind"] == "control-stop-verified"), None)
control_sent = next((r for r in peerlog if r["kind"] == "control-handshake-sent"), None)
control_disconnected = next((r for r in peerlog if r["kind"] == "control-disconnected"), None)
result["rop_lifecycle_verified"] = args.case != "registry-rop-disconnect" or bool(
    bound
    and authenticated
    and survived
    and stopped
    and control_sent
    and control_disconnected
    and bound["rop_id"] == survived["rop_id"] == stopped["rop_id"]
    and bound["entry_marker"] == survived["entry_marker"]
    and bound["session_marker"] == survived["session_marker"] == stopped["session_marker"]
    and bound["controller_marker"] == survived["controller_marker"]
    and bound["channel"] == authenticated["channel"] == survived["channel"] == control_sent["channel"]
    and authenticated["helper_pid"] == survived["helper_pid"] == control_sent["pid"]
    and bound["socket_port"] == survived["socket_port"]
)
result["same_session"] = bool(result.get("received")) and result["received"]["session_marker"] == next(
    (r["session_marker"] for r in rows if r["kind"] == "expected"), None
)
result["no_manual_reopen"] = (
    not any((r["kind"] == "manual-reopen-required" for r in rows)) if args.case not in ("resume-close",) else True
)
result["measurement_complete"] = bool(
    process.returncode == 0
    and any(r.get("result") == "measured" for r in result["result_events"])
    and result["expected_frame_verified"]
    and result["runtime_identity"] is not None
    and result["native_loaded_verified"]
)
gates = (
    "measurement_complete",
    "strict_all_calls_under_1s",
    "strict_all_timed_calls_under_1s",
    "heartbeats_while_held",
    "intermediate_layouts_absent",
    "registry_error_seen",
    "retained_frame_verified",
    "fresh_connection",
    "retained_on_fresh_connection",
    "same_session",
    "no_manual_reopen",
    "rop_lifecycle_verified",
)
result["passed_gates"] = {name: bool(result[name]) for name in gates}
result["result"] = (
    "passed"
    if (
        all(result["passed_gates"].values())
        and result["cleanup"]["all_owned_descendants_exited_without_force_kill"]
        and result["cleanup"]["all_private_peer_threads_exited"]
        and not killed
    )
    else "failed"
)
(D / "receipt.json").write_text(json.dumps(result, indent=2))
print(
    json.dumps(
        {
            k: result.get(k)
            for k in [
                "case",
                "result",
                "child_exit",
                "strict_all_calls_under_1s",
                "strict_all_timed_calls_under_1s",
                "heartbeats_while_held",
                "retained_frame_verified",
                "expected_frame_verified",
                "native_loaded",
            ]
        }
    )
)
sys.exit(0 if result["result"] == "passed" else 1)
