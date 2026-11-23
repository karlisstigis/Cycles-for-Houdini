# SPDX-License-Identifier: Apache-2.0
"""Persistent SOHO control process. It owns no geometry, images or renderer device."""

import json
import os
import socket
import sys
import threading
from pathlib import Path


connection = None


def trace(event, **fields):
    report = os.environ.get("CYCLES_HOUDINI_GUI_REPORT")
    if report:
        with Path(report).with_suffix(".pipe.log").open("a", encoding="utf-8") as log:
            log.write(json.dumps(dict(event=event, pid=os.getpid(), **fields)) + "\n")


def watch_host(sock):
    # The host closing the session also retires the otherwise blocked pipe process.
    try:
        while sock.recv(1024):
            pass
    except OSError:
        pass
    os._exit(0)


try:
    trace("started")
    for line in sys.stdin:
        trace("line", length=len(line), json=line.startswith("{"))
        if not line.startswith("{"):
            continue  # SOHO may prefix its mandatory Houdini license statement.
        message = json.loads(line)
        if message.get("protocol") != 1:
            raise RuntimeError("Unsupported Cycles control protocol")
        if connection is None:
            if message.get("event") != "bind":
                raise RuntimeError("The first control message must bind a session")
            host, port = message["endpoint"]
            if host != "127.0.0.1":
                raise RuntimeError("Only a local Houdini control endpoint is supported")
            connection = socket.create_connection((host, int(port)), timeout=5)
            trace("connected", port=int(port))
            connection.settimeout(None)
            message["pid"] = os.getpid()
            connection.sendall((json.dumps(message) + "\n").encode("utf-8"))
            trace("bound")
            threading.Thread(target=watch_host, args=(connection,), daemon=True).start()
        else:
            connection.sendall((json.dumps(message) + "\n").encode("utf-8"))
finally:
    trace("finished")
    if connection is not None:
        connection.close()
