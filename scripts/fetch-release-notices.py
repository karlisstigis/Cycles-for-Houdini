# SPDX-License-Identifier: Apache-2.0
"""Capture pinned upstream notices with immutable source/hash receipts.

Only this maintenance command uses the network. Package audit/build/install are
offline. A captured source license is not a redistribution approval for an SDK.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORIES = {
    "embree": ("RenderKit/embree", "v4.4.1", "4.4.1", "Apache-2.0"),
    "epoxy": ("anholt/libepoxy", "1.5.10", "1.5.10", "MIT"),
    "fmt": ("fmtlib/fmt", "12.1.0", "12.1.0", "MIT"),
    "level-zero": ("oneapi-src/level-zero", "v1.29.0", "v1.29.0", "MIT AND third-party notices"),
    "houdini-sdk-openimagedenoise": (
        "RenderKit/oidn",
        "v2.3.3",
        "2.3.3",
        "Apache-2.0; external Houdini SDK runtime is not redistributed",
    ),
    "pugixml": ("zeux/pugixml", "v1.10", "1.10", "MIT"),
    "zlib": ("madler/zlib", "v1.3.1", "1.3.1", "Zlib"),
    "zstd": ("facebook/zstd", "v1.5.7", "1.5.7", "BSD-3-Clause OR GPL-2.0-only"),
    "dpcpp-spirv-headers": (
        "KhronosGroup/SPIRV-Headers",
        "9268f3057354a2cb65991ba5f38b16d81e803692",
        "9268f3057354a2cb65991ba5f38b16d81e803692",
        "MIT-Khronos-old",
    ),
    "opencl-headers": ("KhronosGroup/OpenCL-Headers", "v2025.07.22", "v2025.07.22", "Apache-2.0"),
    "opencl-icd-loader": ("KhronosGroup/OpenCL-ICD-Loader", "v2025.07.22", "v2025.07.22", "Apache-2.0"),
    "vc-intrinsics": (
        "intel/vc-intrinsics",
        "60cea7590bd022d95f5cf336ee765033bd114d69",
        "60cea7590bd022d95f5cf336ee765033bd114d69",
        "MIT",
    ),
    "emhash": (
        "ktprime/emhash",
        "5e131ba09a5290823fe71099d9c35eb5df5345b6",
        "5e131ba09a5290823fe71099d9c35eb5df5345b6",
        "MIT",
    ),
    "unified-memory-framework": (
        "oneapi-src/unified-memory-framework",
        "v1.1.0",
        "v1.1.0",
        "Apache-2.0 WITH LLVM-exception",
    ),
    "hwloc": ("open-mpi/hwloc", "hwloc-2.10.0", "2.10.0", "BSD-3-Clause AND bundled notices"),
    "parallel-hashmap": (
        "greg7mdp/parallel-hashmap",
        "8a889d3699b3c09ade435641fb034427f3fd12b6",
        "8a889d3699b3c09ade435641fb034427f3fd12b6",
        "Apache-2.0 AND bundled notices",
    ),
}
BLENDER_RECIPE_REVISION = "f78a5e153839490a9e179969d247af2d58fb82be"
DPCPP_REQUIRES = (
    "dpcpp-spirv-headers",
    "opencl-headers",
    "opencl-icd-loader",
    "vc-intrinsics",
    "emhash",
    "unified-memory-framework",
    "parallel-hashmap",
    "level-zero",
    "zstd",
    "hwloc",
)
NOTICE = re.compile(r"^(?:copying(?:\..*)?|.*licen[cs]e(?:[._-].*)?|notice(?:[._-].*)?|copyright(?:[._-].*)?)$", re.I)
OPENPGL_NOTICES = (
    "LICENSE.txt",
    "third-party-programs.txt",
    "third-party-programs-Embree.txt",
    "third-party-programs-ISPC.txt",
    "third-party-programs-OIDN.txt",
    "third-party-programs-oneTBB.txt",
)


def is_notice(name):
    return NOTICE.match(Path(name).name) or "licenses" in [part.lower() for part in Path(name).parts]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def openpgl(source):
    """Capture the original notices from the exact pinned binary dependency tree."""
    dependency = json.loads((source / "source.lock.json").read_text(encoding="utf-8"))["windows_dependencies"]
    revision = dependency["commit"]
    root = source / "third_party/cycles/lib/windows_x64"

    def pinned(name):
        spec = revision + ":" + name
        value = subprocess.check_output(["git", "-C", str(root), "show", spec])
        blob = subprocess.check_output(["git", "-C", str(root), "rev-parse", spec]).decode().strip()
        observed = (
            subprocess.check_output(["git", "-C", str(root), "hash-object", "--path=" + name, str(root / name)])
            .decode()
            .strip()
        )
        if (root / name).read_bytes() != value and observed != blob:
            raise ValueError("OpenPGL dependency file differs from the pinned Git blob: " + name)
        return value, dict(repository=dependency["url"], revision=revision, path=name, git_blob=blob)

    version_header, version_source = pinned("openpgl/include/openpgl/version.h")
    version = re.search(rb'^#define OPENPGL_VERSION "([0-9.]+)"', version_header, re.M)
    if not version:
        raise ValueError("OpenPGL version header has no recognized version")
    recipe, recipe_source = pinned("deps.md")
    version = version.group(1).decode("ascii")
    if not re.search(rb"\| `OPENPGL` \| `v?" + version.encode() + rb"` \|", recipe):
        raise ValueError("OpenPGL dependency recipe and header version disagree")
    files = []
    for name in OPENPGL_NOTICES:
        value, receipt = pinned("openpgl/share/doc/openpgl/" + name)
        files.append((name, value, receipt))
    return (
        "openpgl",
        dict(
            version=version,
            license="Apache-2.0 AND bundled third-party notices",
            coverage="complete",
            provenance=dict(
                repository=dependency["url"],
                revision=revision,
                version_header=dict(version_source, sha256=sha(version_header)),
                dependency_recipe=dict(recipe_source, sha256=sha(recipe)),
                scope="Original license and third-party program notices from the pinned precompiled OpenPGL distribution",
            ),
            files=[],
        ),
        files,
    )


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Cycles-Houdini-notice-inventory/1"})
    with urllib.request.urlopen(request, timeout=35) as response:
        data = response.read(32 * 1024**2 + 1)
    if len(data) > 32 * 1024**2:
        raise RuntimeError("Notice response is larger than the bounded inventory limit: " + url)
    return data


def github(component, spec):
    repository, ref, version, license_name = spec
    url = f"https://api.github.com/repos/{repository}/git/trees/{ref}?recursive=1"
    tree = json.loads(fetch(url))
    if tree.get("truncated"):
        raise RuntimeError("Truncated upstream notice inventory: " + component)
    commit = (
        ref
        if re.fullmatch("[0-9a-f]{40}", ref)
        else json.loads(fetch(f"https://api.github.com/repos/{repository}/commits/{ref}"))["sha"]
    )
    notices = [entry["path"] for entry in tree["tree"] if entry["type"] == "blob" and is_notice(entry["path"])]
    if not notices:
        raise RuntimeError("No upstream license files: " + component)
    records = []
    for name in sorted(notices):
        source = f"https://raw.githubusercontent.com/{repository}/{commit}/{name}"
        records.append((name, fetch(source), {"url": source, "revision": commit}))
    return (
        component,
        dict(
            version=version,
            license=license_name,
            coverage="complete",
            provenance={
                "repository": "https://github.com/" + repository,
                "ref": ref,
                "revision": commit,
                "tree_sha256": sha(json.dumps(tree, sort_keys=True).encode()),
            },
            files=[],
        ),
        records,
    )


def dpcpp():
    repository, ref = "intel/llvm", "v7.1.0"
    commit = json.loads(fetch(f"https://api.github.com/repos/{repository}/commits/{ref}"))["sha"]
    records = []
    # Compiler/runtime root notices, plus the separately licensed Unified Runtime
    # source tree used by ur_loader and adapter DLLs. Avoid a truncated LLVM tree.
    roots = ("", "sycl", "unified-runtime", "xpti", "xptifw")
    for directory in roots:
        entries = json.loads(fetch(f"https://api.github.com/repos/{repository}/contents/{directory}?ref={commit}"))
        for entry in entries:
            if entry["type"] == "file" and NOTICE.match(entry["name"]):
                source = f"https://raw.githubusercontent.com/{repository}/{commit}/{entry['path']}"
                records.append((entry["path"], fetch(source), {"url": source, "revision": commit}))
    return (
        "dpcpp",
        dict(
            version=ref,
            license="Apache-2.0 WITH LLVM-exception AND third-party notices",
            coverage="complete",
            requires=list(DPCPP_REQUIRES),
            provenance={"repository": "https://github.com/" + repository, "ref": ref, "revision": commit},
            files=[],
        ),
        records,
    )


def eigen():
    revision = "8a1083e9bf41b91fdea6546681f806154efdc25a"
    records = []
    for name in ("COPYING.README", "COPYING.MPL2", "COPYING.BSD", "COPYING.MINPACK", "COPYING.APACHE"):
        url = f"https://gitlab.com/libeigen/eigen/-/raw/{revision}/{name}"
        records.append((name, fetch(url), {"url": url, "revision": revision}))
    return (
        "eigen",
        dict(
            version=revision,
            license="MPL-2.0 AND bundled permissive notices",
            coverage="complete",
            provenance={"repository": "https://gitlab.com/libeigen/eigen", "revision": revision},
            files=[],
        ),
        records,
    )


def pthreads():
    url = "https://downloads.sourceforge.net/project/pthreads4w/pthreads4w-code-v3.0.0.zip"
    data = fetch(url)
    records = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            if not name.endswith("/") and NOTICE.match(Path(name).name):
                records.append((name, archive.read(name), {"url": url, "archive_sha256": sha(data), "member": name}))
    if not records:
        raise RuntimeError("No license text in the authoritative pthreads4w 3.0.0 archive")
    return (
        "pthreads",
        dict(
            version="3.0.0",
            license="Apache-2.0",
            coverage="complete",
            provenance={"distribution": url, "archive_sha256": sha(data)},
            files=[],
        ),
        records,
    )


def optix_header_notices(sdk, source):
    """Collect notice blocks only, including the copyright years for each header."""
    groups = {}
    inputs = {}
    for header in sorted((sdk / "include").rglob("*.h")):
        data = header.read_bytes()
        text = data.decode("utf-8").lstrip()
        block, end, _ = text.partition("*/")
        if not (
            text.startswith("/*") and end and "SPDX-FileCopyrightText:" in block and "SPDX-License-Identifier:" in block
        ):
            raise ValueError(f"Review the OptiX header notice format in {header}")
        lines = [line.strip().removeprefix("*").lstrip() for line in block[2:].splitlines()]
        notice = "\n".join(lines).strip()
        name = header.relative_to(sdk).as_posix()
        inputs[name] = sha(data)
        groups.setdefault(notice, []).append(name)
    if "include/optix_stubs.h" not in inputs:
        raise ValueError("Missing NVIDIA OptiX loader headers")
    content = "\n\n".join("\n".join(names) + "\n\n" + notice for notice, names in groups.items())
    return (
        "HEADER-NOTICES.txt",
        (content + "\n").encode("utf-8"),
        {"root": sdk.relative_to(source).as_posix(), "header_sha256": inputs},
    )


def local(source):
    lock = json.loads((source / "source.lock.json").read_text(encoding="utf-8"))
    records = []
    for component, version, license_name, root, paths in (
        ("integration", "current source", "Apache-2.0", source, [source / "LICENSE"]),
        (
            "cycles",
            lock["cycles"]["commit"],
            "Apache-2.0 AND bundled source notices",
            source / "third_party/cycles",
            [
                source / "third_party/cycles/LICENSE",
                source / "third_party/cycles/AUTHORS",
                *sorted((source / "third_party/cycles/src/doc/license").glob("*.txt")),
                *sorted((source / "third_party/cycles/third_party").glob("*/LICENSE")),
            ],
        ),
        (
            "optix-headers",
            lock["optix_headers"]["commit"],
            "NVIDIA DesignWorks agreement and per-header licenses",
            source / "third_party/optix",
            [source / "third_party/optix/LICENSE.txt", source / "third_party/optix/license_info.txt"],
        ),
        (
            "dlss-headers",
            lock["dlss_headers"]["commit"],
            "NVIDIA RTX SDKs License",
            source / "third_party/dlss",
            [source / "third_party/dlss/LICENSE.txt"],
        ),
    ):
        files = [
            (path.relative_to(root).as_posix(), path.read_bytes(), {"path": path.relative_to(source).as_posix()})
            for path in paths
        ]
        coverage = "review_required" if component in ("optix-headers", "dlss-headers") else "complete"
        item = dict(
            version=version,
            license=license_name,
            coverage=coverage,
            provenance={"source_lock_sha256": sha((source / "source.lock.json").read_bytes())},
            files=[],
        )
        if component == "optix-headers":
            files.append(optix_header_notices(root, source))
            item["reason"] = (
                "Whoever distributes compiled packages reviews the NVIDIA OptiX SDK distribution terms (DesignWorks agreement) first."
            )
        if component == "dlss-headers":
            item["reason"] = (
                "Header-only NGX loader compiled into the runtime. Review SDK attribution before public distribution."
            )
        records.append((component, item, files))
    vendor = source / "packaging/notices/pybind11"
    receipt = json.loads((vendor / "source.json").read_bytes())
    headers = source / "include/third_party/pybind11"
    observed = {path.relative_to(headers).as_posix(): sha(path.read_bytes()) for path in headers.rglob("*.h")}
    if observed != receipt["files"]:
        raise ValueError("Vendored pybind11 headers differ from the pinned source receipt")
    records.append(
        (
            "pybind11",
            dict(
                version=receipt["version"],
                license=receipt["license"],
                coverage="complete",
                provenance={
                    "source": receipt["source"],
                    "header_receipt_sha256": sha((vendor / "source.json").read_bytes()),
                },
                files=[],
            ),
            [
                (
                    "LICENSE.txt",
                    (vendor / "LICENSE.txt").read_bytes(),
                    {"path": "packaging/notices/pybind11/LICENSE.txt"},
                )
            ],
        )
    )
    # The GPU programs are compiled with NVIDIA's CUDA compiler; no CUDA Toolkit file is packaged.
    # Project-written notice (the CUDA EULA does not say that its text may be passed on).
    cuda = source / "packaging/notices/cuda/NOTICE.txt"
    records.append(
        (
            "cuda",
            dict(
                version=lock["certification_candidate"]["cuda_toolkit"],
                license="NVIDIA CUDA Toolkit End User License Agreement (https://docs.nvidia.com/cuda/eula/index.html)",
                coverage="complete",
                provenance={
                    "source_lock_sha256": sha((source / "source.lock.json").read_bytes()),
                    "redistributed": False,
                },
                files=[],
            ),
            [("NOTICE.txt", cuda.read_bytes(), {"path": "packaging/notices/cuda/NOTICE.txt"})],
        )
    )
    return records


def recipe_provenance():
    """Capture references, not recipe GPL source, in the runtime notice inventory."""
    result = {}
    for name in ("versions", "dpcpp"):
        url = f"https://raw.githubusercontent.com/blender/blender/{BLENDER_RECIPE_REVISION}/build_files/build_environment/cmake/{name}.cmake"
        value = fetch(url)
        if name == "versions":
            for token in (b"set(DPCPP_VERSION v7.1.0)", b"set(OIDN_VERSION 2.5.0)", b"set(LEVEL_ZERO_VERSION v1.29.0)"):
                if token not in value:
                    raise RuntimeError("Historical recipe no longer matches the pinned dependency inventory")
        result[name] = dict(url=url, sha256=sha(value), revision=BLENDER_RECIPE_REVISION)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--component", action="append", help="Refresh only selected components, preserving the remaining inventory"
    )
    args = parser.parse_args()
    source, output = args.source_root.resolve(), args.output_root.resolve()
    if not output.is_relative_to(source):
        parser.error("Notice output must stay inside the selected source workspace")
    output.mkdir(parents=True, exist_ok=True)
    results = [entry for entry in local(source) if not args.component or entry[0] in args.component]
    if not args.component or "openpgl" in args.component:
        results.append(openpgl(source))
    errors = []
    jobs = [lambda c=c, s=s: github(c, s) for c, s in REPOSITORIES.items() if not args.component or c in args.component]
    jobs += [
        job
        for name, job in (("dpcpp", dpcpp), ("eigen", eigen), ("pthreads", pthreads))
        if not args.component or name in args.component
    ]

    def attempt(job):
        try:
            return job()
        except Exception as error:
            return error

    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(attempt, jobs):
            if isinstance(result, Exception):
                errors.append(str(result))
            else:
                results.append(result)
    previous = (
        json.loads((output / "index.json").read_text(encoding="utf-8"))
        if args.component and (output / "index.json").exists()
        else {}
    )
    index = dict(
        schema_version=1,
        dependency_recipe=dict(
            path="third_party/cycles/lib/windows_x64/deps.md",
            sha256=sha((source / "third_party/cycles/lib/windows_x64/deps.md").read_bytes()),
        ),
        components=previous.get("components", {}),
        errors=errors,
    )
    # Retired vendor 2.5 is deliberately not a selected or redistributed runtime.
    index["components"].pop("openimagedenoise", None)
    for component, record, files in results:
        for name, value, receipt in files:
            # All names come from the reviewed upstream tree; still reject traversal.
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("Invalid upstream notice path")
            destination = output / component / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
            record["files"].append(
                dict(
                    path=destination.relative_to(source).as_posix(),
                    target=relative.as_posix(),
                    bytes=len(value),
                    sha256=sha(value),
                    source=receipt,
                )
            )
        index["components"][component] = record
    if (
        args.component
        and set(args.component) <= {"optix-headers", "dlss-headers", "pybind11", "openpgl", "cuda"}
        and previous.get("build_recipe_references")
    ):
        index["build_recipe_references"] = previous["build_recipe_references"]
    else:
        index["build_recipe_references"] = recipe_provenance()
    if "dpcpp" in index["components"]:
        index["components"]["dpcpp"]["provenance"]["build_recipe_reference"] = index["build_recipe_references"]["dpcpp"]
    if "houdini-sdk-openimagedenoise" in index["components"]:
        record = index["components"]["houdini-sdk-openimagedenoise"]
        record["provenance"]["sdk_provider"] = json.loads((source / "source.lock.json").read_text(encoding="utf-8"))[
            "sdk_dependencies"
        ]["openimagedenoise"]
        record["provenance"]["redistributed"] = False
        record["provenance"]["scope"] = (
            "Upstream header/source notices only; SideFX-provided DLLs remain in the licensed Houdini installation"
        )
    (output / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            dict(
                result="passed" if not errors else "blocked",
                output=str(output),
                components=sorted(index["components"]),
                errors=errors,
            )
        )
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
