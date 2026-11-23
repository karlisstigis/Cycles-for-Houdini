# SPDX-License-Identifier: Apache-2.0
"""Check the two explicit failed-edit outcomes without confusing requested and accepted scenes."""


def failed_update(entry, accepted_generation, fragment):
    if not entry or not entry.error:
        return False
    status = entry.session.status()
    if status.get("update_rejected"):
        # A renderer validation ACK guarantees that the candidate never changed
        # the accepted scene. Its requested generation must not label that image.
        assert status["state"] == "failed" and status["backend_alive"], status
        assert status["committed_generation"] == accepted_generation, status
        assert status["generation"] > accepted_generation, status
        assert status["backend_state"] in ("rendering", "complete", "paused"), status
    else:
        # A host cook/graph failure has no safely accepted replacement. Wait
        # for the actual Stop ACK, rather than a queued command or old pixels.
        if status["state"] != "cancelled":
            return False
        assert status["generation"] == accepted_generation, status
    assert fragment in entry.error, (fragment, entry.error)
    frame = entry.session.frame_info()
    assert frame and frame["generation"] == accepted_generation, frame
    return True


def recovered_update(entry, accepted_generation, fragment):
    """A known source problem must publish a new usable image and a warning."""
    if not entry:
        return False
    status = entry.session.status()
    assert not entry.error and status["state"] != "failed", (entry.error, status)
    frame = entry.session.frame_info()
    if not frame or frame["generation"] <= accepted_generation or status["first_image_seconds"] <= 0:
        return False
    if frame["generation"] != status.get("committed_generation", status["generation"]):
        return False
    fragment = "missing" if fragment == "Missing Cycles material" else fragment.lower()
    assert any(fragment in issue["message"].lower() for issue in status["source_diagnostics"]), (fragment, status)
    return True
