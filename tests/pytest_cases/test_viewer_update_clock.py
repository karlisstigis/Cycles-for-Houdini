# SPDX-License-Identifier: Apache-2.0
"""The viewer's time counts from the artist's action (viewer/update_clock.py).

Statuses below follow the worker session's contract: ``generation`` is the
requested generation, ``committed_generation`` the one the renderer accepted,
``elapsed_seconds`` the renderer's own sampling time of the committed image.
"""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType

import pytest

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

VIEWER = Path(__file__).resolve().parents[2] / "houdini/python3.13libs/cycles_houdini/viewer"


def load():
    # A stand-in package so the module's relative import of viewer_timing works.
    package = ModuleType("viewer_under_test")
    package.__path__ = [str(VIEWER)]
    sys.modules["viewer_under_test"] = package
    spec = importlib.util.spec_from_file_location("viewer_under_test.update_clock", VIEWER / "update_clock.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def uc():
    return load()


def status(generation, committed=None, state="rendering", elapsed=0.0, pending=False, **extra):
    committed = generation if committed is None else committed
    return dict(
        generation=generation,
        committed_generation=committed,
        state=state,
        elapsed_seconds=elapsed,
        pending_update=pending or committed < generation,
        current_sample=extra.pop("current_sample", 1 if elapsed else 0),
        progress=extra.pop("progress", 0.5 if elapsed else 0.0),
        first_image_seconds=extra.pop("first_image", 0.0),
        **extra,
    )


def test_initial_render_counts_loading_before_sampling(uc):
    clock = uc.UpdateClock(clock=lambda: 0.0)
    clock.trigger("render", 10.0)
    clock.observe(status(0, state="idle"), 10.1, waiting=True)
    view = clock.view(status(0, state="idle"), 10.1)
    assert view["phase"] == "loading" and view["total"] == pytest.approx(0.1)
    # render() reserved generation 1; the cook and transfer are still running.
    clock.observe(status(1, committed=0), 12.0)
    assert clock.view(status(1, committed=0), 12.0)["total"] == pytest.approx(2.0)
    # Committed at 12.5 (observed at 12.6 with 0.1 s of sampling).
    running = status(1, elapsed=0.1, cooking_seconds=0.3, extraction_seconds=0.05)
    clock.observe(running, 12.6, details=lambda generation: dict(transfer=0.2))
    view = clock.view(running, 12.6)
    assert view["phase"] == "render"
    assert view["loading"] == pytest.approx(2.5)
    assert view["total"] == pytest.approx(2.6)
    assert view["parts"] == dict(cook=0.3, extract=0.05, transfer=0.2)
    later = status(1, elapsed=4.0)
    assert clock.view(later, 16.5)["total"] == pytest.approx(6.5)
    # The renderer's pure render time is untouched and reported separately.
    assert clock.view(later, 16.5)["render"] == 4.0
    done = status(1, state="complete", elapsed=5.0, first_image=0.4)
    clock.observe(done, 18.0)
    view = clock.view(done, 18.0)
    assert not view["open"] and view["total"] == pytest.approx(7.5) and view["render"] == 5.0
    # A finished render keeps its final total while idle.
    assert clock.view(done, 99.0)["total"] == pytest.approx(7.5)
    assert clock.history[-1]["outcome"] == "complete" and clock.history[-1]["total"] == pytest.approx(7.5)


def test_bound_but_not_sampling_yet_shows_loading(uc):
    clock = uc.UpdateClock()
    clock.trigger("edit", 0.0)
    clock.observe(status(1), 1.0)  # Committed, the renderer is still building its scene.
    building = status(1, elapsed=0.2, current_sample=0, progress=0.0)
    view = clock.view(building, 1.2)
    assert view["phase"] == "loading" and view["total"] == pytest.approx(1.2)
    assert clock.view(status(1, elapsed=0.3), 1.3)["phase"] == "render"


def test_edit_without_reload_shows_small_honest_number(uc):
    clock = uc.UpdateClock()
    clock.trigger("render", 0.0)
    clock.observe(status(1, state="complete", elapsed=9.0), 10.0)
    assert clock.view(status(1, state="complete", elapsed=9.0), 10.0)["total"] == pytest.approx(10.0)
    clock.trigger("edit", 20.0)
    assert clock.view(status(1, state="complete", elapsed=9.0), 20.1)["phase"] == "loading"
    # A reused (unchanged) render keeps reporting the previous elapsed time.
    reused = status(2, state="complete", elapsed=9.0)
    clock.observe(reused, 20.3)
    view = clock.view(reused, 20.3)
    assert not view["open"] and view["total"] == pytest.approx(0.3)
    assert view["render"] is None or view["render"] == pytest.approx(9.0)


def test_coalesced_edits_count_from_the_latest_edit(uc):
    clock = uc.UpdateClock()
    clock.observe(status(3, state="complete", elapsed=1.0), 0.0)
    clock.trigger("edit", 1.0)
    clock.trigger("edit", 1.5)
    assert clock.current.triggers == 2
    clock.observe(status(4, elapsed=0.1), 2.0)
    assert clock.view(status(4, elapsed=0.1), 2.0)["loading"] == pytest.approx(0.4)


def test_edit_during_render_closes_the_previous_update(uc):
    clock = uc.UpdateClock()
    clock.trigger("render", 0.0)
    clock.observe(status(1, elapsed=0.5), 1.0)
    clock.trigger("frame", 3.0)
    assert clock.history[-1]["outcome"] == "superseded"
    assert clock.view(status(1, elapsed=2.5), 3.1)["phase"] == "loading"


def test_no_change_keeps_the_previous_total(uc):
    clock = uc.UpdateClock()
    clock.trigger("render", 0.0)
    clock.observe(status(1, state="complete", elapsed=2.0), 3.0)
    clock.trigger("edit", 10.0)
    clock.observe(status(1, state="complete", elapsed=2.0), 10.1, waiting=True)
    clock.observe(status(1, state="complete", elapsed=2.0), 10.3)
    assert clock.view(status(1, state="complete", elapsed=2.0), 10.3)["phase"] == "loading"
    clock.observe(status(1, state="complete", elapsed=2.0), 10.6)
    view = clock.view(status(1, state="complete", elapsed=2.0), 10.6)
    assert not view["open"] and view["total"] == pytest.approx(3.0)
    assert clock.history[-1]["outcome"] == "no change"


def test_stop_while_loading_freezes_the_wait(uc):
    clock = uc.UpdateClock()
    clock.trigger("render", 0.0)
    clock.observe(status(1, committed=0), 1.0)
    cancelled = status(1, committed=0, state="cancelled")
    cancelled["pending_update"] = False
    clock.observe(cancelled, 2.0)
    assert clock.view(cancelled, 50.0)["total"] == pytest.approx(2.0)
    assert clock.history[-1]["outcome"] == "cancelled"


def test_untracked_generation_starts_its_own_clock(uc):
    clock = uc.UpdateClock()
    assert clock.view(status(1, elapsed=3.0), 0.0) is None  # Legacy: renderer time only.
    clock.observe(status(1, state="complete", elapsed=3.0), 0.0)
    clock.observe(status(2, committed=1), 5.0)  # Another viewer applied an edit.
    assert clock.view(status(2, committed=1), 6.0)["total"] == pytest.approx(1.0)


def test_rebase_ignores_other_session_generations(uc):
    clock = uc.UpdateClock()
    clock.observe(status(7, state="complete", elapsed=1.0), 0.0)
    clock.trigger("render", 1.0)
    clock.rebase()
    clock.observe(status(1, elapsed=0.2), 2.0)
    assert clock.view(status(1, elapsed=0.2), 2.0)["loading"] == pytest.approx(0.8)


def test_history_is_bounded(uc):
    clock = uc.UpdateClock()
    for generation in range(1, 30):
        clock.trigger("edit", generation * 10.0)
        clock.observe(status(generation, state="complete", elapsed=1.0), generation * 10.0 + 2.0)
    assert len(clock.history) == uc.HISTORY


def test_tooltip_splits_time_with_short_labels(uc):
    view = dict(total=7.5, kind="render", loading=2.5, first_image=0.4, parts=dict(cook=0.3, transfer=0.2), open=False)
    text = uc.tooltip(view, 5.0)
    assert text.splitlines() == [
        "Total 00:07.50 since Render",
        "Loading 2.50 s (cook 0.30 · transfer 0.20)",
        "First image 0.40 s after start",
        "Render 5.00 s",
    ]
    assert uc.tooltip(None, 1.25) == "Render 1.25 s"
    # A part that ran while an earlier preview of the edit was rendering is not loading.
    view.update(loading=0.01, parts=dict(wait=0.16, cook=0.0), first_image=None)
    assert uc.tooltip(view, 5.0).splitlines()[1] == "Loading 0.01 s (cook 0.00)"


def test_first_image_arrives_after_the_render_started(uc):
    clock = uc.UpdateClock()
    clock.trigger("edit", 0.0)
    clock.observe(status(1, elapsed=0.05), 0.5)
    assert clock.view(status(1, elapsed=0.05), 0.5)["first_image"] is None
    clock.observe(status(1, elapsed=0.3, first_image=0.2), 0.75)
    assert clock.view(status(1, elapsed=0.3), 0.75)["first_image"] == 0.2
