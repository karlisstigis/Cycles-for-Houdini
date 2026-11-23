# SPDX-License-Identifier: Apache-2.0
import os
import sys
import runpy
from pathlib import Path
import hou

root = Path(os.environ["CH_PEER_ROOT"])
sys.path.insert(0, str(root / "tests"))


def start():
    from native_runtime_identity import verify

    verify()
    runpy.run_path(os.environ["CH_PEER_CHILD_SOURCE"], run_name="__main__")


def ready():
    hou.ui.removeEventLoopCallback(ready)
    hou.ui.postEventCallback(start)


hou.ui.addEventLoopCallback(ready)
