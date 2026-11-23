# SPDX-License-Identifier: Apache-2.0
"""Host-only negative controls for the soak's retention and board proof."""

from copy import deepcopy
import ctypes as ct
import statistics
import unittest
from unittest.mock import patch

import pytest

from session_soak_fixture import cuda_board, retention_window, selected_adapter

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


class Symbol:
    def __init__(self, implementation):
        self.implementation = implementation

    def __call__(self, *args):
        self.implementation(*args)
        return 0


class SessionSoakFixtureTest(unittest.TestCase):
    def test_disjoint_settled_windows_expose_growth_hidden_by_overlap(self):
        mib = 2**20
        old = [0, 0, 0, 0, 90 * mib, 90 * mib]
        self.assertLessEqual(statistics.median(old[-4:]) - statistics.median(old[:4]), 64 * mib)
        samples = [{"label": f"settled {index}", "time": 60 * index} for index in (1, 2, 3)]
        observed = retention_window(samples, old[1::2])
        self.assertGreater(observed["median_growth_bytes"], 64 * mib)
        self.assertEqual(observed["early_labels"], ["settled 1"])
        self.assertEqual(observed["late_labels"], ["settled 3"])

    def test_dwell_windows_report_actual_ranges_and_observation_count(self):
        samples = [{"label": str(index), "time": 15 * index} for index in range(5)]
        observed = retention_window(samples, [10, 14, 12, 26, 40])
        self.assertEqual(observed["observations"], 5)
        self.assertEqual(observed["observations_per_window"], 2)
        self.assertEqual(observed["early_range_bytes"], [10, 14])
        self.assertEqual(observed["late_range_bytes"], [26, 40])
        self.assertEqual(observed["late_seconds"], [45, 60])
        self.assertEqual(observed["median_growth_bytes"], 21)

    def test_cuda_pci_location_pins_the_luid_even_when_device_order_differs(self):
        ids = (b"0000:01:00.0", b"0000:03:00.0")

        def luid(result, mask, device):
            result[0], result[1] = 0x12345 + device.value, 0
            mask._obj.value = 1

        class Driver:
            cuInit = Symbol(lambda _: None)
            cuDeviceGetCount = Symbol(lambda size: setattr(size._obj, "value", len(ids)))
            cuDeviceGet = Symbol(lambda device, index: setattr(device._obj, "value", index))
            cuDeviceGetPCIBusId = Symbol(lambda raw, _size, device: setattr(raw, "value", ids[device.value]))
            cuDeviceGetLuid = Symbol(luid)
            cuDeviceGetUuid = Symbol(lambda result, device: result.__setitem__(0, device.value + 1))

        with patch.object(ct, "WinDLL", return_value=Driver(), create=True):
            selected = cuda_board("CUDA_GPU_0000:03:00_OptiX")
            self.assertEqual(selected["pci_bus_id"], "0000:03:00.0")
            self.assertEqual(selected["luid"], "00000000:00012346")
            self.assertEqual(selected["uuid"], "02" + "00" * 15)
            with self.assertRaises(AssertionError):
                cuda_board("CUDA_GPU_0000:99:00_OptiX")
        with patch.object(ct, "WinDLL", create=True) as library:
            with self.assertRaises(AssertionError):
                cuda_board("CPU")
            library.assert_not_called()

    def test_same_name_other_gpu_cannot_supply_target_usage(self):
        board = dict(luid="00000000:00012346", node=0)
        adapter = {
            "name": "GPU",
            "luid": board["luid"],
            "node": 0,
            "vendor_id": 0x10DE,
            "local": {"current_usage_bytes": 0},
            "nonlocal": {"current_usage_bytes": 0},
        }
        other = dict(adapter, luid="00000000:00012345", local={"current_usage_bytes": 99})
        memory = dict(gpu=dict(adapters=[other, adapter]))
        with self.assertRaises(AssertionError):
            selected_adapter(memory, board, require_usage=True)
        self.assertEqual(selected_adapter(memory, board), {"local": 0, "nonlocal": 0})
        adapter["local"]["current_usage_bytes"] = 12
        self.assertEqual(selected_adapter(memory, board, require_usage=True), {"local": 12, "nonlocal": 0})
        absent = deepcopy(memory)
        absent["gpu"]["adapters"].pop()
        with self.assertRaises(AssertionError):
            selected_adapter(absent, board)
        adapter["nonlocal"] = {"unavailable_hresult": -1}
        with self.assertRaises(AssertionError):
            selected_adapter(memory, board)
