# SPDX-License-Identifier: Apache-2.0
"""Native moving emitter and independent spatial/temporal coverage integration."""

import functools
import hou, numpy as np
from camera_lens_motion_fixture import Fixture as LensFixture


class Fixture(LensFixture):
    def __init__(self, device):
        super().__init__(device)
        self.grid.parmTuple("size").set((0.1, 20))
        self.sop.parm("snippet").set("@P.x+=@Time*12;")
        self.camera.parm("tx").deleteAllKeyframes()
        self.camera.setParms(dict(tx=0, projection=1, orthowidth=4))
        self.rop.setParms(dict(dof_mode="off", samples=4096, ipr_samples=4096))
        self.set_curve("uniform")
        self.set_rolling(False, 0.1)

    def set_rolling(self, enabled, duration):
        self.camera.parm("cycles_rolling_duration").deleteAllKeyframes()
        self.camera.setParms(
            dict(cycles_rolling_shutter="top" if enabled else "none", cycles_rolling_duration=duration)
        )

    def set_curve(self, shape):
        values = {
            "uniform": (1, 1),
            "rise": (0, 1),
            "fall": (1, 0),
            "triangle": (0, 1, 0),
            "negative": (-1, 1),
            "zero": (0, 0),
        }[shape]
        self.camera.parm("cycles_use_shutter_curve").set(shape != "uniform")
        self.camera.parm("cycles_shutter_curve").set(
            hou.Ramp(
                (hou.rampBasis.Linear,) * len(values), tuple(i / (len(values) - 1) for i in range(len(values))), values
            )
        )


@functools.lru_cache(maxsize=128)
def reference(shape="uniform", rolling=False, duration=0.1, offset=0, rate=12.0, camera_x=0.0, width=160, height=96):
    """Box-filtered bar coverage integrated against independent analytic PDFs.

    No Cycles camera, LUT, curve resolver, render output or engine helper builds
    the expected image. The scan law is a linear time offset per integer row.
    """
    d = duration if rolling else 1.0
    scale = width / 4
    travel = rate * 0.5 / 24 * scale
    half = 0.05 * scale
    y, x = np.indices((height, width), dtype=np.float64)
    start = width / 2 - camera_x * scale + offset * travel / 2 + travel * (-d * 0.5 + (1 - d) * (0.5 - y / height))
    spread = travel * d
    if spread == 0:
        coverage = np.maximum(0, np.minimum(x + 1, start + half) - np.maximum(x, start - half))
    else:
        # Antiderivatives of the normalized temporal CDFs. Spatial box
        # integration of their difference gives exact pixel coverage.
        def integral(z):
            t = np.clip(z, 0, 1)
            tail = np.maximum(z - 1, 0)
            if shape == "uniform":
                return t * t / 2 + tail
            if shape == "rise":
                return t * t * t / 3 + tail
            if shape == "fall":
                return t * t - t * t * t / 3 + tail
            if shape == "triangle":
                return (2 * t * t * t - 4 * np.maximum(t - 0.5, 0) ** 3) / 3 + tail
            raise ValueError(shape)

        coverage = spread * (
            integral((x + 1 + half - start) / spread)
            - integral((x + half - start) / spread)
            - integral((x + 1 - half - start) / spread)
            + integral((x - half - start) / spread)
        )
    return (np.clip(coverage, 0, 1)[:, :, None] * np.array((0.2, 0.4, 0.8))).astype(np.float32)
