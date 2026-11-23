# SPDX-License-Identifier: Apache-2.0
"""Capture identical XML scenes with pristine and fork standalone Cycles CLIs."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

import numpy as np

from cycles_cli_backend import describe as describe_backend, require_like_backends
from heavy_bias_statistics import BIAS_SEEDS


CASES = {
    "diffuse_cube": "scene_cube_surface.xml",
    "glossy_glass": "scene_monkey.xml",
    "glass_caustics": "scene_caustics.xml",
}
WIDTH, HEIGHT = 96, 64
FORK_DEFAULTS = (
    r'SOCKET_INT\(pt_ris_nee,\s*"Resampled Light Sampling",\s*0\)',
    r'SOCKET_BOOLEAN\(nested_dielectrics,\s*"Nested Dielectrics",\s*false\)',
    r'SOCKET_BOOLEAN\(use_guiding,\s*"Guiding",\s*false\)',
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def place_lights(root):
    """Move ``<light tfm=...>`` placements into a ``<transform>`` the pinned XML reader honours.

    Upstream examples (scene_caustics.xml) still position lights with a ``tfm`` attribute on
    ``<light>``. Since lights became object geometry, the reader builds the light object from
    the enclosing transform state and drops ``tfm``, which leaves the light at the origin
    (inside scene_caustics' ground plane). Returns the translations that were applied.
    """
    moved = []
    parents = {child: parent for parent in root.iter() for child in parent}
    for light in list(root.iter("light")):
        matrix = light.attrib.pop("tfm", None)
        if matrix is None:
            continue
        values = [float(value) for value in matrix.replace(",", " ").split()]
        if len(values) != 12 or [values[i] for i in (0, 1, 2, 4, 5, 6, 8, 9, 10)] != [1, 0, 0, 0, 1, 0, 0, 0, 1]:
            raise ValueError(f"Only translation-only light tfm placements are supported: {matrix!r}")
        translate = f"{values[3]:g} {values[7]:g} {values[11]:g}"
        parent = parents[light]
        index = list(parent).index(light)
        wrapper = ET.Element("transform", translate=translate)
        wrapper.text, wrapper.tail = light.tail, light.tail
        parent.remove(light)
        wrapper.append(light)
        parent.insert(index, wrapper)
        moved.append(dict(light=light.get("name", ""), translate=translate))
    return moved


def prepare(source, output):
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Bias output is not empty: {output}")
    examples = source / "third_party/cycles/examples"
    integrator_source = source / "third_party/cycles/src/scene/integrator.cpp"
    source_text = integrator_source.read_text(encoding="utf-8")
    if not all(re.search(pattern, source_text) for pattern in FORK_DEFAULTS):
        raise RuntimeError("Fork's RIS, nested-media or guiding defaults changed; review Bias baseline")
    destination = output / "inputs"
    objects = destination / "objects"
    objects.mkdir(parents=True, exist_ok=True)
    for name in ("cube.xml", "sphere.xml", "suzanne.xml"):
        shutil.copyfile(examples / "objects" / name, objects / name)
    corrections = {}
    for case, filename in CASES.items():
        original = examples / filename
        for seed in BIAS_SEEDS:
            root = ET.parse(original).getroot()
            corrections[case] = place_lights(root)
            integrator = root.find("integrator")
            if integrator is None:
                integrator = ET.Element("integrator")
                root.insert(0, integrator)
            # Preserve a common stock/fork XML input and common random numbers.
            integrator.set("seed", str(seed))
            integrator.set("use_guiding", "false")
            xml = destination / f"{case}-{seed}.xml"
            ET.ElementTree(root).write(xml, encoding="utf-8", xml_declaration=True)
    manifest = dict(
        run_id=uuid.uuid4().hex,
        source_examples={name: digest(examples / filename) for name, filename in CASES.items()},
        includes={name: digest(objects / name) for name in ("cube.xml", "sphere.xml", "suzanne.xml")},
        inputs={f"{case}-{seed}": digest(destination / f"{case}-{seed}.xml") for case in CASES for seed in BIAS_SEEDS},
        fork_integrator_sha256=digest(integrator_source),
        light_placements=corrections,
    )
    (output / "inputs.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def read_exr(path):
    import OpenImageIO as oiio

    source = oiio.ImageInput.open(str(path))
    if source is None:
        raise RuntimeError(f"Cannot open Cycles EXR: {path}")
    try:
        spec = source.spec()
        data = np.asarray(source.read_image(oiio.FLOAT), dtype=np.float32)
        data = data.reshape(spec.height, spec.width, spec.nchannels)
        if data.shape[:2] != (HEIGHT, WIDTH) or data.shape[2] < 3:
            raise ValueError(f"Unexpected image dimensions or channels: {data.shape}")
        rgb = data[..., :3].copy()
        if not np.isfinite(rgb).all():
            raise ValueError(f"Non-finite linear RGB output: {path}")
        return rgb
    finally:
        source.close()


def run(source, executable, role, output, *, smoke):
    manifest = json.loads((output / "inputs.json").read_text(encoding="utf-8"))
    integrator_source = source / "third_party/cycles/src/scene/integrator.cpp"
    if digest(integrator_source) != manifest["fork_integrator_sha256"]:
        raise ValueError("Fork integrator defaults source changed after Bias preparation")
    for name, expected in manifest["includes"].items():
        if digest(output / "inputs" / "objects" / name) != expected:
            raise ValueError(f"Bias XML include changed after preparation: {name}")
    target = output / role
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"Bias {role} output is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    exe_hash = digest(executable)
    backend = describe_backend(executable, os.environ.get("PATH", ""))
    print(json.dumps(dict(role=role, bvh_backend=backend)), flush=True)
    if role == "fork":
        recorded = sorted((output / "stock").glob("*.json"))
        if recorded:
            stock_backend = json.loads(recorded[0].read_text(encoding="utf-8")).get("bvh_backend")
            require_like_backends(stock_backend, backend)
    base_keys = [f"{case}-{seed}" for case in CASES for seed in BIAS_SEEDS]
    keys = base_keys
    if smoke:
        keys = [f"diffuse_cube-{BIAS_SEEDS[0]}"]
    for key in keys:
        seed = int(key.rsplit("-", 1)[1])
        case = key.rsplit("-", 1)[0]
        xml = output / "inputs" / f"{key}.xml"
        if digest(xml) != manifest["inputs"][key]:
            raise ValueError("Authored XML changed after preparation")
        exr = target / f"{key}.exr"
        command = [
            str(executable),
            "--background",
            "--quiet",
            "--device",
            "CPU",
            "--samples",
            "8" if smoke else "16384",
            "--width",
            str(WIDTH),
            "--height",
            str(HEIGHT),
            "--output",
            str(exr),
            str(xml),
        ]
        start = time.monotonic()
        subprocess.run(command, check=True, timeout=300 if smoke else 14400, env=os.environ.copy())
        wall = time.monotonic() - start
        image = read_exr(exr)
        array_path = target / f"{key}.npy"
        np.save(array_path, image)
        metadata = dict(
            role=role,
            run_id=manifest["run_id"],
            case=case,
            seed=seed,
            samples=8 if smoke else 16384,
            input_sha256=manifest["inputs"][key],
            image_sha256=digest(array_path),
            executable_sha256=exe_hash,
            executable=str(executable),
            bvh_backend=backend,
            wall_seconds=wall,
            mean_rgb=image.mean((0, 1)).tolist(),
        )
        (target / f"{key}.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(json.dumps(metadata), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--role", choices=("stock", "fork"))
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        prepare(args.source, args.output)
    else:
        if not args.executable or not args.role:
            parser.error("Capture requires --executable and --role")
        run(args.source, args.executable, args.role, args.output, smoke=args.smoke)


if __name__ == "__main__":
    main()
