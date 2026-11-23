# SPDX-License-Identifier: Apache-2.0
"""The parameter menu consumes cached worker capabilities and never calls native device probes."""

from support.package_loader import load_module
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace

root = Path(__file__).resolve().parents[1] / "houdini/python3.13libs/cycles_houdini"
package = ModuleType("dlss_menu_host")
package.__path__ = [str(root)]
sys.modules[package.__name__] = package
sys.modules["hou"] = SimpleNamespace(time=lambda: 0)
rop = SimpleNamespace(sessionId=lambda: 123)
package.device_selection = SimpleNamespace(selected_devices=lambda rop, now: ["gpu"])
controller = SimpleNamespace(condition=threading.Condition(), cached={})
package.registry = SimpleNamespace(get=lambda key: SimpleNamespace(session=SimpleNamespace(_controller=controller)))
module = load_module(package, "dlss")
assert "pending" in module.denoiser_menu(rop)[-1]
controller.cached["devices"] = [dict(id="gpu", denoisers=["dlss"])]
assert module.denoiser_menu(rop)[-1] == "DLSS Ray Reconstruction"
controller.cached["devices"] = [dict(id="gpu", denoisers=["oidn_gpu"])]
assert "unavailable" in module.denoiser_menu(rop)[-1]
controller.cached["devices"] = [dict(id="other-gpu", denoisers=["dlss"])]
assert "unavailable" in module.denoiser_menu(rop)[-1]
controller.cached["worker_startup_failure"] = "DLSS runtime rejected the request"
assert "startup failed" in module.denoiser_menu(rop)[-1]
controller.cached["devices"] = []
assert "startup failed" in module.denoiser_menu(rop)[-1]
package.registry.get = lambda key: None
assert "pending" in module.denoiser_menu(rop)[-1]
print("Passed: cached worker support, unavailable selection, and pending without native/NGX UI probing")
