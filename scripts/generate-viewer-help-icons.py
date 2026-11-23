# SPDX-License-Identifier: Apache-2.0
"""Publish the Viewer's toolbar glyphs as inline Houdini help illustrations."""

from pathlib import Path
import re
import sys
from xml.etree import ElementTree as ET

from cycles_houdini.viewer_icons import svg_source


def publish(help_root):
    guide = (help_root / "cycles/viewer.txt").read_text(encoding="utf-8")
    names = set(re.findall(r"\[Icon:/images/cycles-viewer/([\w-]+)\.svg\]", guide))
    if not names:
        raise ValueError("No toolbar icons found in the Cycles Viewer guide")

    target = help_root / "images/cycles-viewer"
    target.mkdir(parents=True, exist_ok=True)
    ns = "{http://www.w3.org/2000/svg}"
    ET.register_namespace("", ns[1:-1])
    for name in sorted(names):
        source = svg_source(name, "#f5f5f5")
        if not source:
            raise ValueError(f"Unknown Cycles Viewer glyph in help: {name}")
        # Keep the light strokes legible on both light and dark help themes.
        tile = ET.Element(ns + "svg", width="24", height="24", viewBox="0 0 24 24")
        ET.SubElement(tile, ns + "rect", width="24", height="24", rx="4", fill="#383838")
        drawing = ET.fromstring(source)
        drawing.attrib.update(x="2", y="2", width="20", height="20")
        tile.append(drawing)
        ET.ElementTree(tile).write(target / (name + ".svg"), encoding="utf-8")


if __name__ == "__main__":
    publish(Path(sys.argv[1]))
