# SPDX-License-Identifier: Apache-2.0
"""The viewer's status row before the first sample shows what is happening, like Blender.

On 23 Nov 2026 a first-time GPU kernel compile of several minutes showed only "Loading · Time: 02:12.49":
the row keeps the first two " · " parts of the headline (viewer_timing.summary) and the engine's text came
third. Now, as in Blender, the time comes first and the stage second, in full; "Loading" alone is never
shown. The row elides at its right end, so a narrow row loses the added sentence before the engine's words.
"""

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

VIEWER = Path(__file__).resolve().parents[2] / "houdini/python3.13libs/cycles_houdini/viewer"


@pytest.fixture
def timing():
    spec = importlib.util.spec_from_file_location("viewer_timing_under_test", VIEWER / "viewer_timing.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_kernel_loading_text_is_what_the_row_shows(timing):
    engine = "Loading render kernels (may take a few minutes the first time). Done once per new GPU feature."
    headline = timing.loading_headline(timing.text(132.49), engine)
    assert timing.summary(headline) == "Time: 02:12.49 · " + engine


@pytest.mark.parametrize(
    "message,shown",
    [
        ("", "Waiting for the renderer"),
        (None, "Waiting for the renderer"),
        ("Sample 0/1024", "Waiting for the renderer"),
        ("Scene update queued", "Waiting for the renderer to take the queued scene update"),
        (
            "Updating Mesh / Tessellating sop:13/material-mode:native/smooth:0/subdivision:2:1:1 1/1",
            "Updating Mesh / Tessellating 1/1",
        ),
        ("Loading denoising kernels (may take a few minutes the first time)", None),
        ("Updating Device / Writing constant memory", None),
    ],
)
def test_stage_is_never_an_empty_loading(timing, message, shown):
    stage = timing.loading_stage(message)
    assert stage == (shown if shown is not None else message)
    row = timing.summary(timing.loading_headline("Time: 00:01.00", message))
    assert row == "Time: 00:01.00 · " + stage and stage and stage != "Loading"


def test_a_stage_with_the_separator_stays_in_one_part(timing):
    row = timing.summary(timing.loading_headline("Time: 00:02.00", "A · B"))
    assert row == "Time: 00:02.00 · A - B"


def test_worker_start_failure_reason_survives_the_visible_summary(timing):
    message = "worker did not load the SDK OIDN provider"
    headline = timing.failure_headline("Time: 00:30.00", f"Cycles Houdini SDK dependency: {message}. Log: worker.log")
    assert timing.summary(headline) == f"Failed · {message} · Time: 00:30.00"
    headline = timing.failure_headline("Time: 00:30.00", "first · stage " + "x" * 500)
    assert timing.summary(headline) == headline and headline.startswith("Failed · first - stage ")
    assert len(headline) < 180
