# SPDX-License-Identifier: Apache-2.0
import pytest
from support.displayed_frame_metrics import ActionPaints, distribution

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


def frame(g=8, r=5, epoch=10, samples=1, size=(640, 360), sequence=20):
    return dict(
        generation=g,
        render_revision=r,
        display_epoch=epoch,
        samples=samples,
        rendered_width=size[0],
        rendered_height=size[1],
        sequence=sequence,
    )


def observe(action, metadata, *, session="a", status=None, epoch=10, now=2):
    status = status or dict(generation=metadata["generation"], render_revision=metadata["render_revision"])
    return action.observe(now, session, metadata["generation"], metadata, status, epoch)


def test_old_generation_and_repaint_never_finish_new_action():
    action = ActionPaints(1, "a", frame(), (640, 360))
    assert not observe(action, frame(sequence=21))
    assert not observe(action, frame(epoch=11), epoch=11)  # Display setting changed only.
    assert not observe(action, frame(g=9))  # Logical no-op ACK with same physical revision.
    assert not observe(action, frame(), status=dict(generation=9, render_revision=6))
    assert action.first_visible is None
    assert observe(action, frame(g=8, r=6))  # Same logical generation, new physical revision.
    assert action.first_full_resolution_sample["seconds"] == 1


def test_replacement_counter_reset_requires_new_display_epoch():
    action = ActionPaints(1, "a", frame(), (640, 360), allow_replacement=True)
    assert not observe(action, frame(g=1, r=1, epoch=9), session="b")
    assert observe(action, frame(g=1, r=1), session="b")
    strict = ActionPaints(1, "a", frame(), (640, 360))
    assert not observe(strict, frame(g=1, r=1), session="b")


def test_first_visible_and_full_resolution_are_separate_and_sample_qualified():
    action = ActionPaints(1, None, {}, (640, 360), allow_replacement=True)
    assert not observe(action, frame(samples=0))
    assert not observe(action, frame(), now=0.9)
    assert not observe(action, frame(), status=dict(generation=8, render_revision=5, pending_update=True))
    assert observe(action, frame(size=(320, 180)), now=1.1)
    assert action.first_full_resolution_sample is None
    assert observe(action, frame(samples=2), now=1.4)
    assert action.first_visible["seconds"] == pytest.approx(0.1)
    assert action.first_full_resolution_sample["seconds"] == pytest.approx(0.4)


def test_distribution_does_not_turn_unavailable_metrics_into_zero():
    assert distribution([None])["median"] is None
    assert distribution([3, 1, 2, None]) == dict(count=3, median=2, p95=3, maximum=3)
