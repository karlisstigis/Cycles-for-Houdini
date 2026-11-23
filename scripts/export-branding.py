# SPDX-License-Identifier: Apache-2.0
"""Export the project logo (a PDF-compatible Illustrator file). Not a build dependency.

Requires PyMuPDF and Pillow. The committed exports are used by release builds;
Illustrator, PDF libraries and source artwork are not needed at install time.
Use --icon-only to generate installer.ico from the published Render ROP SVG.
"""

from pathlib import Path
import argparse
import io
import xml.etree.ElementTree as ET

import pymupdf
from PIL import Image


def export_installer_icon(source: Path, output: Path):
    # Keep the existing ROP artwork for the Windows application icon.
    # The full wordmark remains legible in the welcome and header areas.
    icon_svg = source.read_bytes()
    icon_doc = pymupdf.open(stream=icon_svg, filetype="svg")
    icon_scale = 256 / max(icon_doc[0].rect.width, icon_doc[0].rect.height)
    pix = icon_doc[0].get_pixmap(matrix=pymupdf.Matrix(icon_scale, icon_scale), alpha=True)
    icon = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGBA")
    icon.thumbnail((240, 240), Image.Resampling.LANCZOS)
    square = Image.new("RGBA", (256, 256))
    square.alpha_composite(icon, ((256 - icon.width) // 2, (256 - icon.height) // 2))
    square.save(output, sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--source", type=Path, default=root / "UI_mockup/CyclesHoudini_Logo_v2.ai")
    parser.add_argument("--output", type=Path, default=root / "houdini/config/branding")
    parser.add_argument(
        "--icon-only", action="store_true", help="Generate installer.ico from the published Render ROP SVG"
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    icon_source = root / "houdini/config/Icons/cycles_render_rop.svg"
    icon_output = args.output / "installer.ico"
    if args.icon_only:
        export_installer_icon(icon_source, icon_output)
        return
    doc = pymupdf.open(args.source)
    page = doc[0]
    svg = ET.fromstring(page.get_svg_image())
    ns = "{http://www.w3.org/2000/svg}"
    ET.register_namespace("", ns[1:-1])
    # Illustrator's page-sized black backdrop is not part of the lettering.
    # Remove only that direct child, retaining all visible-layer vector paths.
    backdrop = svg.find(ns + "path")
    assert backdrop is not None and backdrop.get("fill", "#000000") == "#000000"
    svg.remove(backdrop)
    transparent = pymupdf.open(stream=ET.tostring(svg), filetype="svg")
    png = transparent[0].get_pixmap(matrix=pymupdf.Matrix(6, 6), alpha=True).tobytes("png")
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    bbox = image.getchannel("A").getbbox()
    assert bbox
    pad = 8
    rect = (bbox[0] / 6 - pad, bbox[1] / 6 - pad, (bbox[2] - bbox[0]) / 6 + 2 * pad, (bbox[3] - bbox[1]) / 6 + 2 * pad)
    svg.set("viewBox", " ".join(f"{v:.6f}" for v in rect))
    svg.set("width", f"{rect[2]:.6f}")
    svg.set("height", f"{rect[3]:.6f}")
    svg_path = args.output / "cycles-houdini-logo.svg"
    svg_path.write_bytes(ET.tostring(svg, encoding="utf-8", xml_declaration=True))
    exported = pymupdf.open(stream=svg_path.read_bytes(), filetype="svg")
    logo = Image.open(
        io.BytesIO(exported[0].get_pixmap(matrix=pymupdf.Matrix(6, 6), alpha=True).tobytes("png"))
    ).convert("RGBA")
    logo.save(args.output / "cycles-houdini-logo.png")
    # High-DPI wizard artwork uses the documented 164:314 welcome aspect.
    wizard = Image.new("RGB", (656, 1256), "#181818")
    wordmark = logo.copy()
    wordmark.thumbnail((568, 300), Image.Resampling.LANCZOS)
    wizard.paste(wordmark, ((656 - wordmark.width) // 2, 150), wordmark)
    wizard.save(args.output / "installer-welcome.png")
    # Header is deliberately wide; InitializeWizard maintains this aspect.
    header = Image.new("RGBA", (600, 200))
    wordmark = logo.copy()
    wordmark.thumbnail((560, 180), Image.Resampling.LANCZOS)
    header.alpha_composite(wordmark, ((600 - wordmark.width) // 2, (200 - wordmark.height) // 2))
    header.save(args.output / "installer-header.png")
    export_installer_icon(icon_source, icon_output)


if __name__ == "__main__":
    main()
