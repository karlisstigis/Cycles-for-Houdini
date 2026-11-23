# SPDX-License-Identifier: Apache-2.0
"""Native scalar string and borrowed script-buffer argument compatibility."""

import ctypes
import json
import argparse
from pathlib import Path

import hou
from cycles_houdini import _native


parser = argparse.ArgumentParser()
parser.add_argument("--device", choices=("CPU",), default="CPU", help="Host-only script-buffer compatibility")
parser.add_argument("--report", type=Path)
args = parser.parse_args()
node = hou.node("/obj").createNode("null", "argument_buffer_contract")
checks = []


def accepted(name, value):
    result = _native.scene_definition_fallback(node.path(), value, 0, 0)
    assert result == dict(registered=False, unowned=True), result
    checks.append(dict(name=name, accepted=True))


def rejected(name, call, kind):
    try:
        call()
    except kind as error:
        checks.append(dict(name=name, error_type=type(error).__name__))
    else:
        raise AssertionError(name + " unexpectedly accepted")


try:
    accepted("Unicode", "script")
    accepted("Unicode embedded NUL", "before\0after")
    accepted("Bytes embedded NUL", b"before\0after")
    accepted("Borrowable ctypes buffer", ctypes.create_string_buffer(b"before\0after"))
    for name, value in (
        ("Writable bytearray", bytearray(b"script")),
        ("Read-only memoryview", memoryview(b"script")),
        ("Writable memoryview", memoryview(bytearray(b"script"))),
        ("Noncontiguous memoryview", memoryview(b"script")[::2]),
    ):
        rejected(name, lambda value=value: _native.scene_definition_fallback(node.path(), value, 0, 0), TypeError)
    rejected("Path embedded NUL", lambda: _native.scene_definition_fallback(node.path() + "\0", b"s", 0, 0), ValueError)
    rejected("Path bytes", lambda: _native.scene_definition_fallback(node.path().encode(), b"s", 0, 0), TypeError)
    rejected(
        "Connector overflow", lambda: _native.scene_definition_fallback(node.path(), b"s", 2**100, 0), OverflowError
    )
finally:
    node.destroy()

report = dict(
    result="passed",
    scope="Host-only scalar and borrowed script-buffer compatibility",
    device=args.device,
    checks=checks,
)
if args.report:
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))
print(json.dumps(report))
