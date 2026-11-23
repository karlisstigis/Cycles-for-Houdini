# SPDX-License-Identifier: Apache-2.0
"""Transport ownership regressions; does not require or access a Houdini session."""

from support.package_loader import import_product
import json
import os
from pathlib import Path
import socket
import time
import unittest

path = Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini/ipr/control_bridge.py"
module = import_product("control_bridge")


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = module.ControlBridge()
        self.message = self.bridge.bind("own-test-render")
        self.clients = []

    def tearDown(self):
        self.bridge.close()
        for client in self.clients:
            client.close()
        self.assertFalse(self.bridge._accept_thread.is_alive())
        self.assertFalse(any(thread.is_alive() for thread in self.bridge._readers))

    def connect(self, message=None):
        client = socket.create_connection(self.bridge.endpoint, timeout=2)
        self.clients.append(client)
        data = dict(self.message, pid=os.getpid()) if message is None else message
        client.sendall(json.dumps(data).encode("utf-8") + b"\n")
        return client

    def event(self, expected):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            events = self.bridge.drain()
            match = [item for item in events if item[2] == expected]
            if match:
                return match[0]
            time.sleep(0.005)
        self.fail(f"No {expected} control event")

    def test_eof_retires_bound_channel(self):
        client = self.connect()
        event = self.event("connected")
        self.assertEqual(event, ("own-test-render", self.message["channel"], "connected", os.getpid()))
        client.close()
        self.assertEqual(self.event("closed")[:2], event[:2])
        self.assertEqual(self.bridge.drain(), [])

    def test_duplicate_connection_does_not_retire_original(self):
        original = self.connect()
        self.event("connected")
        duplicate = self.connect()
        self.assertEqual(duplicate.recv(1), b"")
        self.assertEqual(self.bridge.drain(), [])
        original.sendall(json.dumps(dict(protocol=1, channel=self.message["channel"], event="update")).encode() + b"\n")
        self.assertIn(self.message["channel"], self.bridge._bound)
        original.close()
        self.event("closed")

    def test_invalid_handshakes_do_not_create_lifetime_events(self):
        invalid = [
            [],
            dict(self.message, pid=True),
            dict(self.message, pid=-1),
            dict(self.message, pid=os.getpid(), token="invalid"),
        ]
        for message in invalid:
            client = self.connect(message)
            self.assertEqual(client.recv(1), b"")
            self.assertEqual(self.bridge.drain(), [])

    def test_unbind_interrupts_blocked_reader_and_close_joins_threads(self):
        client = self.connect()
        self.event("connected")
        self.bridge.unbind(self.message["channel"])
        self.assertEqual(client.recv(1), b"")
        self.bridge.close()
        self.assertEqual(self.bridge.drain(), [])


if __name__ == "__main__":
    unittest.main()
