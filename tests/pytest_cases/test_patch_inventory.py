# SPDX-License-Identifier: Apache-2.0
"""Portable patch-order and byte-identity contracts, without an engine checkout."""

import hashlib
import json
from pathlib import Path

import pytest

from support.paths import ROOT

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def test_patch_inventory_matches_locked_order_and_exact_bytes():
    lock = json.loads((ROOT / "source.lock.json").read_text())["cycles"]
    inventory = json.loads((ROOT / "patches/inventory.json").read_text())
    assert inventory["engine_commit"] == lock["commit"]
    assert inventory["patch_count"] == len(lock["patches"])
    assert [patch["path"] for patch in inventory["patches"]] == lock["patches"]
    seen = set()
    for patch in inventory["patches"]:
        number = int(Path(patch["path"]).name.split("-", 1)[0])
        assert patch["number"] == number
        path = ROOT / patch["path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == patch["sha256"], patch["path"]
        fixture = patch["regression"]["related_fixture"]
        if fixture is not None:
            assert hashlib.sha256((ROOT / fixture).read_bytes()).hexdigest() == patch["regression"]["fixture_sha256"], (
                fixture,
                "Regenerate the patch inventory with python scripts/patch-inventory.py --write",
            )
        assert set(patch["dependencies"]["shared_file_predecessors"]) <= seen, patch["path"]
        seen.add(number)
