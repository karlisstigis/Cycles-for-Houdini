# SPDX-License-Identifier: Apache-2.0
"""Development API for the native Houdini adapter and shared Cycles session core."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import threading
import time
from .core.execution import resolve as _execution_mode


def _load_native():
    import importlib.machinery
    from .core.sdk_dependencies import check_houdini_edition, validate_runtime

    check_houdini_edition()
    validate_runtime()
    root = Path(os.environ["CYCLES_HOUDINI_HOST_ROOT"])
    path = root / "dso" / "_cycles_houdini.dll"
    loader = importlib.machinery.ExtensionFileLoader("_cycles_houdini", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    validate_runtime(require_loaded=True)
    module.initialize(os.environ["CYCLES_HOUDINI_RESOURCES"])
    validate_runtime(require_loaded=True)
    return module


from .core.native import NativeRuntime

_native = NativeRuntime(lambda: _load_native())


def load():
    """Validate, initialize and return the native extension on first use.

    Importing this package and its pure contracts requires neither Houdini nor
    a configured host environment. Native API calls load the runtime lazily.
    """
    return _native.load() if isinstance(_native, NativeRuntime) else _native


def devices():
    return _native.devices()


def capabilities():
    """Implemented build features; production certification is tracked separately."""
    result = _native.capabilities()
    result.update(native_light_linking=True, native_shadow_linking=True, linking_sets=64, world_linking=False)
    from .core.sdk_dependencies import validate_runtime

    result["openimagedenoise_provider"] = validate_runtime(require_loaded=True)
    from .core.blender_bridge import status as blender_bridge_status

    bridge = blender_bridge_status()
    result["blender_bridge"] = bridge["available"]
    result["blender_bridge_enabled"] = bridge["enabled"]
    return result


def color_policy():
    return _native.color_policy()


def synchronize_color_configuration():
    """Observe Houdini's color policy without interrupting independent workers."""
    return _native.synchronize_color_configuration()


def process_memory():
    """Opt-in whole-process Windows/DXGI snapshot; not per-session residency."""
    return json.loads(_native.process_memory_json())


def shader_catalogue():
    """Pinned engine definitions/defaults; Houdini authoring support is separate."""
    return json.loads(_native.shader_catalogue_json())


def create_material(*args, **kwargs):
    from .materials import create_material as create

    return create(*args, **kwargs)


def create_light_material(*args, **kwargs):
    from .materials import create_light_material as create

    return create(*args, **kwargs)


def create_world_material(*args, **kwargs):
    from .materials import create_world_material as create

    return create(*args, **kwargs)


def material_graph(*args, **kwargs):
    from .materials import material_graph as extract

    return extract(*args, **kwargs)


def create_subnet(*args, **kwargs):
    from .materials import create_subnet as create

    return create(*args, **kwargs)


def create_connector(*args, **kwargs):
    from .materials import create_connector as create

    return create(*args, **kwargs)


def promote_parameter(*args, **kwargs):
    from .materials import promote_parameter as promote

    return promote(*args, **kwargs)


def default_device():
    requested = os.environ.get("CYCLES_HOUDINI_DEVICE")
    available = devices()
    if requested:
        if not any(d["id"] == requested for d in available):
            raise RuntimeError(f"Configured Cycles device is unavailable: {requested}")
        return requested
    candidates = [d for d in available if d["backend"] == "OPTIX" and d["name"] == "NVIDIA GeForce RTX 4090"]
    if len(candidates) != 1:
        raise RuntimeError("Select and persist an explicit Cycles device ID; no unique RTX 4090 default is available.")
    return candidates[0]["id"]


class Session:
    def __new__(cls, *args, execution=None, **kwargs):
        mode = _execution_mode(execution)
        if cls is Session and mode == "worker":
            from .ipr.queued import WorkerSession

            return object.__new__(WorkerSession)
        return object.__new__(cls)

    def __init__(
        self,
        device=None,
        width=640,
        height=360,
        samples=64,
        interactive=True,
        collect_diagnostics=False,
        use_texture_cache=True,
        auto_texture_cache=False,
        denoising=None,
        execution=None,
        threads=0,
        passes=None,
        progressive_display=False,
        use_tiling=False,
        tile_size=2048,
        hair_subdivisions=2,
        refit_meshes=False,
    ):
        self.execution = _execution_mode(execution)
        self.device = device if device is not None else default_device()
        self.width, self.height = width, height
        self._preview_pass, self._native_preview = "combined", False
        self._preview_focus = False
        self._handle = _native.create(
            self.device,
            width,
            height,
            samples,
            interactive,
            collect_diagnostics,
            use_texture_cache,
            auto_texture_cache,
            denoising,
            False,
            threads,
            passes,
            progressive_display,
            use_tiling,
            tile_size,
            hair_subdivisions,
            refit_meshes,
        )

    def apply(self, update, at_time=None, *, reuse_if_unchanged=False):
        import hou

        if hou.isUIAvailable() and threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Scene extraction must run on Houdini's host thread.")
        from .blender.prototype_leases import session_leases, split_update

        payload, metadata = split_update(update)
        leases = session_leases(self)
        submission = object()
        try:
            leases.begin(submission, metadata)
            result = _native.apply(
                self._handle, payload, hou.time() if at_time is None else at_time, reuse_if_unchanged
            )
        except BaseException:
            leases.drop(submission)
            raise
        leases.commit(submission)
        return result

    def calibration(self):
        result = _native.fixture(self._handle)
        leases = getattr(self, "_prototype_leases", None)
        if leases is not None:
            leases.clear()
        return result

    def status(self):
        result = _native.status(self._handle)
        result.update(_native.native_stats(self._handle))
        return result

    def diagnostics(self):
        """Opt-in nested engine timers and logical buffer-copy request counters."""
        return _native.diagnostics(self._handle)

    def memory_snapshot(self):
        return process_memory()

    def textures_changed(self):
        """Inspect committed image-file revisions without changing the render."""
        return _native.textures_changed(self._handle)

    def frame(self, after_sequence=0):
        return _native.frame(self._handle, after_sequence)

    def frame_info(self, after_sequence=0):
        """Read metadata without copying full floating-point render planes."""
        return _native.frame_info(self._handle, after_sequence)

    def focus_sample(self, x, y):
        image = _native.focus_frame(self._handle)
        return _native.sample_focus(image, x, y) if image is not None else None

    def output_metadata(self):
        return _native.output_metadata(self._handle)

    def native_object_keys(self):
        """Diagnostic identities for resources owned by the native scene adapter."""
        return _native.native_object_keys(self._handle)

    def native_material_references(self):
        """Owned material identities from the latest native capture attempt."""
        return _native.native_material_references(self._handle)

    def native_material_paths(self):
        """Referenced and failed lookup paths for host change observation."""
        return _native.native_material_paths(self._handle)

    def native_stylesheet_resources(self):
        return _native.native_stylesheet_resources(self._handle)

    def native_cop_sources(self):
        """Active and failed COP resources in the last accepted shader capture."""
        return _native.cop_image_sources(self._handle)

    def pause(self, paused=True):
        _native.pause(self._handle, paused)

    def cancel(self):
        _native.cancel(self._handle)
        leases = getattr(self, "_prototype_leases", None)
        if leases is not None:
            leases.clear()

    def close(self):
        _native.close(self._handle)
        leases = getattr(self, "_prototype_leases", None)
        if leases is not None:
            leases.close()

    def write_exr(self, filename, options=None):
        _native.write_exr(self._handle, os.fspath(filename), options)

    def texture_inputs(self):
        """Committed input spaces and policy sources; this does not read pixels."""
        return _native.texture_inputs(self._handle)

    def display_image(self, **settings):
        return _native.display_image(self._handle, settings)

    def display_frame(self, after_sequence=0):
        """Retain immutable pixels on the host thread for background display."""
        image = _native.owned_frame(self._handle)
        if image is None:
            return None
        info = _native.frame_info(image)
        if info["sequence"] <= after_sequence:
            return None
        return image, info["generation"], info["sequence"]

    def set_feedback(self, viewer, enabled):
        pass  # Ordered synchronous/reference rendering has no presentation gate.

    def display_state(self, after_sequence=0):
        owned = self.display_frame(after_sequence) if after_sequence is not None else None
        generation = _native.status(self._handle)["generation"]
        if owned is not None and owned[1] != generation:
            owned = None
        return generation, generation, owned

    def set_frame_wakeup(self, viewer, callback):
        pass  # Ordered final/reference sessions retain the polling fallback.

    def notify_presented(self, generation, render_revision=None):
        pass

    def _set_preview_pass(self, name, focus=None):
        _native.set_preview_pass(self._handle, name, self._preview_focus if focus is None else focus)

    def set_preview_pass(self, name, *, focus=False):
        if self._preview_pass != name or self._preview_focus != focus:
            self._set_preview_pass("*" if self._native_preview else name, focus)
            self._preview_pass, self._preview_focus = name, focus

    def set_focus_pick(self, enabled):
        self.set_preview_pass(self._preview_pass, focus=bool(enabled))

    def open_native_display(self, destination="ip", socket_port="", *, options=None):
        was_native = self._native_preview
        _native.open_display(self._handle, destination, str(socket_port), options or {})
        self._native_preview = True
        if not was_native:
            self._set_preview_pass("*")

    def present_native(self):
        _native.present(self._handle)

    def native_display_ready(self):
        """Poll delivery without waiting on the display writer or its reaper."""
        return _native.display_ready(self._handle)

    def notify_color_configuration(self):
        # Reference sessions adopt the policy on their next explicit apply.
        # Artist workers receive their own asynchronous control message.
        return False

    def close_native_display(self):
        _native.close_display(self._handle)
        self._native_preview = False
        self._set_preview_pass(self._preview_pass)

    def wait(self, generation=None, timeout=180):
        target = self.status()["generation"] if generation is None else generation
        start = time.monotonic()
        while True:
            status = self.status()
            if status["state"] == "failed":
                raise RuntimeError(status["error"])
            frame = self.frame()
            if (
                frame
                and frame["generation"] == target
                and frame["complete"]
                and frame.get("current_scene", True)
                # The worker publishes frame and status packets independently.
                # Return only once the status for this target has caught up.
                and status["state"] in ("complete", "paused")
                and status["generation"] == target
                and not status.get("pending_update")
                and not status.get("capture_pending")
            ):
                return frame
            if status["generation"] != target:
                raise RuntimeError(f"Render generation {target} was superseded by {status['generation']}")
            if (
                status.get("color_update_pending")
                and not status.get("pending_update")
                and not status.get("capture_pending")
            ):
                raise RuntimeError("Rendering stopped for a color configuration change; apply the pending scene update")
            if status["state"] in ("cancelled", "closed"):
                raise RuntimeError(f"Render {status['state']}")
            if time.monotonic() - start > timeout:
                self.cancel()
                raise TimeoutError(status["message"])
            time.sleep(0.01)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def matrix_from_houdini(matrix, camera=False):
    values = [matrix.at(col, row) for row in range(3) for col in range(4)]
    if camera:
        for row in range(3):
            values[row * 4 + 2] *= -1
    return values


def geometry_request(
    obj,
    sop=None,
    material="default",
    smooth=False,
    at_time=None,
    point_radius=0.05,
    curve_width=0.001,
    motion="transform",
    volume_step_size=0.0,
    material_mode="reject",
    subdivision=None,
    curve_segments=16,
    stylesheets=(),
    point_render_mode="auto",
):
    import hou

    sop = obj.renderNode() if sop is None else sop
    if sop is None:
        raise ValueError(f"No render SOP for {obj.path()}")
    now = hou.time() if at_time is None else at_time
    if material_mode not in ("reject", "uniform", "native"):
        raise ValueError("Unsupported native material assignment mode: " + material_mode)
    if point_render_mode not in ("auto", "spheres", "gaussian_splats"):
        raise ValueError("Unsupported native point render mode: " + point_render_mode)
    geometry_key = (
        f"sop:{sop.sessionId()}/material-mode:native/smooth:{int(smooth)}"
        if material_mode == "native"
        else f"sop:{sop.sessionId()}/material:{material}/smooth:{int(smooth)}"
        + ("/uniform" if material_mode == "uniform" else "")
    )
    geometry_key += "/point-mode:" + point_render_mode
    from .scene.stylesheets import object_binding

    binding = (
        object_binding(obj, now, stylesheets)
        if material_mode == "native"
        else dict(
            object_material_path="",
            object_material_overrides=[],
            stylesheet="",
            stylesheet_errors=[],
            stylesheet_resources=[],
        )
    )
    return dict(
        sop=sop.path(),
        geometry_key=geometry_key,
        object_key=f"obj:{obj.sessionId()}",
        material=material,
        material_mode=material_mode,
        **binding,
        transform=matrix_from_houdini(obj.worldTransformAtTime(now)),
        random_id=obj.sessionId(),
        smooth=smooth,
        point_radius=point_radius,
        curve_width=curve_width,
        point_render_mode=point_render_mode,
        curve_segments=curve_segments,
        object_path=obj.path(),
        motion=motion,
        volume_step_size=volume_step_size,
        object_properties=dict(cryptomatte_object=obj.path(), cryptomatte_asset=obj.path(), linking_id=obj.path()),
        subdivision=dict(subdivision) if subdivision is not None else {"type": "none"},
    )


__all__ = [
    "load",
    "devices",
    "capabilities",
    "color_policy",
    "synchronize_color_configuration",
    "process_memory",
    "shader_catalogue",
    "create_material",
    "create_light_material",
    "create_world_material",
    "material_graph",
    "create_subnet",
    "create_connector",
    "promote_parameter",
    "default_device",
    "Session",
    "matrix_from_houdini",
    "geometry_request",
]
