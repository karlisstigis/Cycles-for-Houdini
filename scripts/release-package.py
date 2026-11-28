# SPDX-License-Identifier: Apache-2.0
"""Deterministic native Windows payloads and transactional Houdini activation.

Uses only the Python standard library. Never copies the installed Houdini
runtime, executes a renderer, or modifies a preference root implicitly.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager, nullcontext, redirect_stdout, redirect_stderr
import hashlib
import importlib.util
import json
import mmap
import os
import posixpath
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import runpy
import secrets
import struct
import sys
from urllib.parse import unquote, urlsplit
import uuid
import zipfile

SCHEMA = 1
PACKAGE_FILE = "cycles_houdini_dev.json"  # Legacy registration; never used for new installs.
QUALIFIED_HOSTS = ("22.0.429", "22.0.466")
APP_NAMESPACE = uuid.UUID("6d3f2b8e-1c4a-4e7b-9a52-3f8c0d17b6a4")
MANIFEST = "release-manifest.json"
BUILD_PROVENANCE = "provenance/build-manifest.json"
CERTIFICATION = "provenance/certification.json"
ASSET_SEAL = "host/runtime-assets.json"
PUBLIC_BINDING = "public_payload_sha256"
PARENT_MANIFEST = "parent_build_manifest_sha256"
PRIVATE_BUILD_FIELDS = frozenset(
    (
        ("compiler", "path"),
        ("houdini_root",),
        ("ocio", "config"),
        ("build_host", "gpus", "[]", "uuid"),
        ("build_host", "gpus", "[]", "pci_bus_id"),
    )
)
HYDRA_DESCRIPTOR = "host/dso/usd_plugins/hdCycles/resources/plugInfo.json"
INSTALL_MARKER = ".cycles-houdini-installation.json"
# Installed path length (Windows paths stay below 260 characters without long-path support). A payload is stored in
# <app folder>\installation\versions\<first 16 hex of its package_id>\ (64 bits of the SHA-256 identity: the full id
# stays in its release-manifest.json and in state.json and is checked there). The budget: an app folder of up to 110
# characters (C:\Users\ + a 20-character user name + \AppData\Local\ = 44, plus the longest default folder name,
# "Cycles for Houdini 22.0.<build> (<32-character build_id>)" = 63, with 3 to spare) and at most 240 characters in
# all, so at most 125 below the app folder; with the 39 of installation\versions\<16>\ that leaves 86 for a path in
# the package. A package with a longer path is not built; Setup refuses an app folder that would make any path it
# writes longer than 259. Not counted: the bytecode Houdini's Python may write next to a .py file later
# (__pycache__/<name>.cpython-313.pyc, 25 characters longer); Python skips writing it when the path does not fit and
# reads the .py instead.
VERSION_FOLDER_CHARS = 16
APP_FOLDER_BUDGET = 110
INSTALLED_TOTAL_BUDGET = 240
BELOW_APP_BUDGET = 125  # 240 - 110 - 1 separator = 129, 4 kept spare
PAYLOAD_PREFIX_CHARS = len("installation/versions/") + VERSION_FOLDER_CHARS + 1  # 39
PACKAGE_PATH_BUDGET = BELOW_APP_BUDGET - PAYLOAD_PREFIX_CHARS  # 86
WINDOWS_PATH_LIMIT = 259
# Notice texts whose path in the package would be longer than this are stored flat in their component's folder
# (notices/<component>/<12 hex of the original path>-<file name>); notices/index.json in the package names the
# original path of every stored notice.
NOTICE_PATH_LIMIT = 64
NOTICE_INDEX = "notices/index.json"
SYSTEM_DLLS = frozenset(
    (
        "kernel32.dll",
        "user32.dll",
        "advapi32.dll",
        "gdi32.dll",
        "shell32.dll",
        "ole32.dll",
        "oleaut32.dll",
        "uuid.dll",
        "comdlg32.dll",
        "winspool.drv",
        "combase.dll",
        "ntdll.dll",
        "ws2_32.dll",
        "bcrypt.dll",
        "crypt32.dll",
        "secur32.dll",
        "rpcrt4.dll",
        "version.dll",
        "winmm.dll",
        "dbghelp.dll",
        "psapi.dll",
        "shlwapi.dll",
        "setupapi.dll",
        "cfgmgr32.dll",
        "dxgi.dll",
        "d3d11.dll",
        "d3d12.dll",
        "opengl32.dll",
        "glu32.dll",
        "imm32.dll",
        "dwmapi.dll",
        "uxtheme.dll",
        "powrprof.dll",
        "iphlpapi.dll",
        "normaliz.dll",
        "winhttp.dll",
        "wtsapi32.dll",
        "mswsock.dll",
        "imagehlp.dll",
        "netapi32.dll",
        "userenv.dll",
        "propsys.dll",
        "dcomp.dll",
        "dwrite.dll",
        "dhcpcsvc.dll",
        "dnsapi.dll",
        "cryptbase.dll",
        "ncrypt.dll",
        "ntasn1.dll",
    )
)
VC_DLLS = re.compile(r"^(vcruntime\d+(?:_\d+)?|msvcp\d+(?:_\d+|_atomic_wait|_codecvt_ids)?|concrt\d+)\.dll$")
DRIVER_DLLS = frozenset(
    ("nvcuda.dll", "nvoptix.dll", "nvml.dll", "amdhip64.dll", "amdhip64_7.dll", "amdocl64.dll", "intelocl64.dll")
)
RUNTIME_COMPONENTS = ("embree", "epoxy", "dpcpp", "level-zero")
OPTIX_NOTICE_FILES = ("LICENSE.txt", "license_info.txt", "HEADER-NOTICES.txt")
OPENPGL_NOTICE_FILES = (
    "LICENSE.txt",
    "third-party-programs.txt",
    "third-party-programs-Embree.txt",
    "third-party-programs-ISPC.txt",
    "third-party-programs-OIDN.txt",
    "third-party-programs-oneTBB.txt",
)
RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


class PackageError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise PackageError(message)


# A build made by someone else from the public sources carries a build identity
# (build-identity.json in the source root, docs/dev/community-builds.md). Without
# one, a package has the official identity below, unchanged.
BUILD_IDENTITY_KEYS = (
    "schema_version",
    "build_id",
    "build_name",
    "publisher",
    "publisher_url",
    "dlss",
    "dlss_project_id",
)
# "on": DLSS with the builder's own NVIDIA project id (dlss_project_id, a GUID generated on
# first use; NVIDIA's DLSS Programming Guide 310.6.0, section 5.2.1: custom engines pass a
# GUID-like project id of their own). "official-project-id-with-permission": the official id
# below, only with its owner's written agreement. "off": built without DLSS.
BUILD_DLSS_CHOICES = ("on", "off", "official-project-id-with-permission")
# The official project id (patches/0077-houdini-dlss-project.patch, ngx_project_id).
OFFICIAL_DLSS_PROJECT_ID = "09f76326-5bc2-49f7-81ed-e0731bd86f13"
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# build_name becomes part of the Start-menu folder name: no characters Windows refuses or
# reads as a folder separator (":" and "/" are allowed only in publisher_url).
BUILD_TEXT = re.compile(r"[A-Za-z0-9 .,()_+-]{1,60}")
BUILD_PUBLISHER_URL = re.compile(r"https?://[A-Za-z0-9._~:/?#@!$&'()*+,;=%-]{1,200}", re.I | re.ASCII)
# The official publisher (packaging/installer/CyclesHoudini.iss). A community build may not
# carry it in any spelling: compared without case, spaces or punctuation.
OFFICIAL_PUBLISHERS = ("The Cycles for Houdini Authors",)
# Keep the official repository in sync with AppPublisherLink in the installer.
OFFICIAL_PUBLISHER_URL = "https://github.com/karlisstigis/Cycles-for-Houdini"
# Reserve whole hosts only for domains dedicated to the official publisher.
OFFICIAL_HOSTS = ()


def _plain(text):
    return re.sub(r"[^0-9a-z]", "", str(text).casefold())


def _is_official_publisher_url(host, path):
    official = urlsplit(OFFICIAL_PUBLISHER_URL)
    if host.removeprefix("www.") != official.hostname:
        return False
    # GitHub identifies the repository by the first two path segments.
    path = posixpath.normpath(unquote(path)).strip("/").casefold()
    repository = "/".join(path.split("/", 2)[:2]).removesuffix(".git")
    return repository == official.path.strip("/").casefold()


DEFAULT_PUBLISHER = "Unnamed community builder"


def prepare_build_identity(path):
    """First use of build-identity.json: fill in what each builder gets of their own.

    An empty build_id becomes "<publisher>-<6 random hex digits>" ("builder-..." while the
    publisher is still the default). Installers with matching IDs and Houdini targets address
    the same application. With dlss "on", an empty dlss_project_id becomes a random GUID.
    Identity validation rejects the official DLSS project ID in the default community mode.
    Values already in the file are never changed. Returns the names it filled in."""
    path = Path(path)
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8-sig"))
    require(isinstance(value, dict), "Build identity must be a JSON object")
    filled = []
    if not value.get("build_id"):
        publisher = value.get("publisher") if isinstance(value.get("publisher"), str) else ""
        stem = (
            "" if _plain(publisher) == _plain(DEFAULT_PUBLISHER) else re.sub(r"[^a-z0-9]+", "-", publisher.casefold())
        )
        stem = stem.strip("-")
        if not stem[:1].isalpha() or not stem.isascii():
            stem = "builder" + ("-" + stem if stem and stem.isascii() else "")
        value["build_id"] = stem[:24].rstrip("-") + "-" + secrets.token_hex(3)
        filled.append("build_id")
    if value.get("dlss") == "on" and not value.get("dlss_project_id"):
        value["dlss_project_id"] = str(uuid.uuid4())  # random GUID, lowercase
        filled.append("dlss_project_id")
    if filled:
        text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        atomic_bytes(path, (text.replace("\n", "\r\n") if b"\r\n" in raw else text).encode("utf-8"))
    return dict(result="passed", filled=filled, build_id=value.get("build_id"))


def community_display_name(build_name):
    """Apps entry and Start-menu name: always says "community build"."""
    label = "Cycles for Houdini - community build"
    return label if _plain(build_name) in ("community", "communitybuild") else label + " (" + build_name + ")"


def build_identity(value):
    """Return the checked build identity of a community build (None for the official build)."""
    if value is None:
        return None
    require(isinstance(value, dict), "Build identity must be a JSON object")
    record = {key: value.get(key) for key in BUILD_IDENTITY_KEYS}
    require(record["schema_version"] == 1, "Build identity schema_version must be 1")
    build_id = record["build_id"]
    require(
        isinstance(build_id, str) and re.fullmatch(r"[a-z][a-z0-9-]{2,31}", build_id) and not build_id.endswith("-"),
        "build_id must be 3 to 32 lowercase letters, digits or '-', starting with a letter",
    )
    for key in ("build_name", "publisher"):
        require(
            isinstance(record[key], str) and BUILD_TEXT.fullmatch(record[key]) and record[key].strip() == record[key],
            f"{key} must be 1 to 60 plain characters (letters, digits, spaces and . , ( ) _ + -)",
        )
    require(
        not any("official" in str(record[key]).casefold() for key in ("build_id", "build_name")),
        "A community build cannot call itself official",
    )
    url = record["publisher_url"]
    require(
        url == "" or (isinstance(url, str) and BUILD_PUBLISHER_URL.fullmatch(url)),
        "publisher_url must be empty or an http(s) address",
    )
    official = [_plain(name) for name in OFFICIAL_PUBLISHERS + OFFICIAL_HOSTS]
    require(
        not any(
            name in _plain(record[key]) for name in official for key in ("build_name", "publisher", "publisher_url")
        ),
        "A community build must carry your own name, not the official publisher's",
    )
    parsed = urlsplit(url)
    host = unquote(parsed.hostname or "").casefold().rstrip(".")
    require(url == "" or host, "publisher_url must include a hostname")
    require(
        not (
            _is_official_publisher_url(host, parsed.path)
            or any(host == item or host.endswith("." + item) for item in OFFICIAL_HOSTS)
        ),
        "publisher_url must be your own web address, not the official one",
    )
    require(record["dlss"] in BUILD_DLSS_CHOICES, "dlss must be one of: " + ", ".join(BUILD_DLSS_CHOICES))
    project = record["dlss_project_id"] = record["dlss_project_id"] or ""
    require(isinstance(project, str), "dlss_project_id must be text")
    if record["dlss"] == "on":
        require(
            GUID.fullmatch(project),
            'dlss "on" needs dlss_project_id, a GUID such as a0f57b54-1daf-4934-90ae-c4035c19df04 '
            "(left empty, it is filled in on first use)",
        )
        require(
            project != OFFICIAL_DLSS_PROJECT_ID,
            'dlss_project_id is the official DLSS project id; use "official-project-id-with-permission" only '
            "with its owner's written agreement",
        )
    else:
        require(project in ("", OFFICIAL_DLSS_PROJECT_ID), 'dlss_project_id is used only with dlss "on"')
    return record


def community_host(houdini):
    """A community build may target any build of the qualified major.minor (22.0.x): its builder
    compiles it for the Houdini build they have. The official route stays on QUALIFIED_HOSTS."""
    families = {".".join(host.split(".")[:2]) for host in QUALIFIED_HOSTS}
    return bool(re.fullmatch(r"\d+\.\d+\.\d+", houdini or "")) and ".".join(houdini.split(".")[:2]) in families


def target_identity(houdini, build=None):
    require(
        houdini in QUALIFIED_HOSTS or (build is not None and community_host(houdini)),
        f"Unqualified Houdini target: {houdini}",
    )
    build = build_identity(build)
    if build is None:
        return dict(
            houdini=houdini,
            platform="windows-x64",
            app_id="{" + str(uuid.uuid5(APP_NAMESPACE, "Cycles for Houdini/windows-x64/" + houdini)).upper() + "}",
            package_filename="cycles_houdini_" + houdini.replace(".", "_") + ".json",
            default_directory="Cycles for Houdini " + houdini,
        )
    # Own Windows application id, folder and Houdini package file: a community
    # build never updates or uninstalls the official one, and the shared package
    # checks below refuse to activate two Cycles builds for one Houdini.
    name = "community/" + build["build_id"] + "/windows-x64/" + houdini
    return dict(
        houdini=houdini,
        platform="windows-x64",
        app_id="{" + str(uuid.uuid5(APP_NAMESPACE, "Cycles for Houdini/" + name)).upper() + "}",
        package_filename="cycles_houdini_community_"
        + build["build_id"].replace("-", "_")
        + "_"
        + houdini.replace(".", "_")
        + ".json",
        default_directory="Cycles for Houdini " + houdini + " (" + build["build_id"] + ")",
        build_id=build["build_id"],
        display_name=community_display_name(build["build_name"]),
        publisher=build["publisher"],
        publisher_url=build["publisher_url"],
    )


def package_enable(houdini, hdk_api=None, installation_root=None, provider=None, additional_roots=()):
    """Expose the DSO only to installed builds with the compiled HDK ABI.

    Houdini discovers DSOs before Python startup, so the runtime Python check
    cannot protect an incompatible host from native plugin loading.
    """
    require(re.fullmatch(r"\d+\.\d+\.\d+", houdini), "An exact Houdini target is required")
    versions = {houdini}
    required_runtime = [item for item in provider.get("files", ()) if item.get("kind") == "runtime"] if provider else []
    require(not additional_roots or hdk_api is not None, "Frozen HDK API is required to refresh registration")
    if hdk_api is not None:
        major_minor = ".".join(houdini.split(".")[:2])
        base = (
            Path(installation_root)
            if installation_root is not None
            else Path(os.environ.get("ProgramFiles", "")) / "Side Effects Software"
        )
        candidates = [(root, False) for root in base.glob("Houdini " + major_minor + ".*")]
        candidates.extend((Path(root), True) for root in additional_roots)
        for root, explicit in candidates:
            try:
                version = re.search(
                    r'(?m)^#define SYS_VERSION_FULL "(\d+\.\d+\.\d+)"',
                    (root / "toolkit/include/SYS/SYS_Version.h").read_text(),
                )[1]
                api = re.search(
                    r"(?m)^#define HDK_API_VERSION\s+(\d+)", (root / "toolkit/include/UT/UT_HDKVersion.h").read_text()
                )[1]
            except (OSError, TypeError):
                require(not explicit, f"Cannot read Houdini version and HDK API headers: {root}")
                continue
            runtime_matches = bool(required_runtime) if provider is not None else True
            for item in required_runtime:
                candidate = root / item["path"]
                if (
                    not candidate.is_file()
                    or candidate.stat().st_size != item["bytes"]
                    or file_hash(candidate) != item["sha256"]
                ):
                    runtime_matches = False
                    break
            if explicit:
                require(version.startswith(major_minor + "."), f"Houdini {version} is outside {major_minor}: {root}")
                require(int(api) == int(hdk_api), f"Houdini {version} HDK API {api} differs from {hdk_api}: {root}")
                require(runtime_matches, f"Houdini {version} required SDK DLL identity differs: {root}")
            if version.startswith(major_minor + ".") and int(api) == int(hdk_api) and runtime_matches:
                versions.add(version)
    exact = f"houdini_version == '{houdini}'"
    alternates = [
        f"(houdini_version == '{version}' and $CYCLES_HOUDINI_STRICT_BUILD != '1')"
        for version in sorted(versions - {houdini})
    ]
    return " or ".join([exact, *alternates])


def package_eligible_versions(expression):
    """Parse only the package eligibility grammar we own; reject unknown gates."""
    if expression is False:
        return set()
    require(isinstance(expression, str), "Unparseable Cycles package eligibility")
    versions = set()
    for part in expression.split(" or "):
        exact = re.fullmatch(r"houdini_version == '(\d+\.\d+\.\d+)'", part)
        alternate = re.fullmatch(
            r"\(houdini_version == '(\d+\.\d+\.\d+)' and \$CYCLES_HOUDINI_STRICT_BUILD != '1'\)", part
        )
        broad = re.fullmatch(r"houdini_version == '(\d+\.\d+)'", part)
        require(exact or alternate or broad, "Unparseable Cycles package eligibility")
        versions.add((exact or alternate or broad)[1] + (".*" if broad else ""))
    return versions


def eligible_sets_overlap(first, second):
    for a in first:
        for b in second:
            if a == b or (a.endswith(".*") and b.startswith(a[:-1])) or (b.endswith(".*") and a.startswith(b[:-1])):
                return True
    return False


def overlaps(first, second):
    first, second = unlinked_path(first), unlinked_path(second)
    return first.is_relative_to(second) or second.is_relative_to(first)


def windows_installations():
    """Read actual uninstall ownership, including the old recursive uninstaller."""
    if os.name != "nt":
        return []
    import winreg

    result = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(
                    hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | view
                ) as entries:
                    for index in range(winreg.QueryInfoKey(entries)[0]):
                        try:
                            with winreg.OpenKey(entries, winreg.EnumKey(entries, index)) as entry:
                                name = winreg.QueryValueEx(entry, "DisplayName")[0]
                                if name.startswith("Cycles for Houdini"):
                                    result.append(
                                        dict(
                                            path=unlinked_path(winreg.QueryValueEx(entry, "InstallLocation")[0]),
                                            app_id=winreg.EnumKey(entries, index).removesuffix("_is1"),
                                        )
                                    )
                        except OSError:
                            continue
            except OSError:
                continue
    return result


def validate_install_paths(
    root, prefs, data, target, application_root=None, isolated=False, planned_enable=None, build=None
):
    """Do not place any new payload under another uninstaller's deletion tree."""
    identity = target_identity(target, build)
    app = unlinked_path(application_root) if application_root else root
    require(not (isolated and application_root), "Isolated verification cannot install a Windows application")
    require(app != Path(app.anchor) and app != Path.home().resolve(), "Choose a dedicated application directory")
    require(root.is_relative_to(app), "Installation root must belong to its application directory")
    require(
        not overlaps(app, prefs) and not overlaps(root, data) and not overlaps(prefs, data),
        "Installation, preferences and mutable data paths must not overlap",
    )
    for ancestor in app.parents:
        nested = ancestor / "installation"
        nested_marker = nested / INSTALL_MARKER
        owns_parent = (
            nested_marker.exists()
            and unlinked_path(read_json(nested_marker).get("application_root", ancestor)) == ancestor
        )
        require(
            not (ancestor / INSTALL_MARKER).exists() and (nested == root or not owns_parent),
            f"Application directory overlaps another installation: {ancestor}",
        )
    for existing in windows_installations():
        existing_path = existing["path"]
        same_target = existing.get("app_id", "").lower() == identity["app_id"].lower()
        if isolated and same_target:
            # Setup owns both the installation and its Houdini preferences.
            # A release verification uses separate payload, data and preferences.
            owner = read_json(existing_path / "installation" / INSTALL_MARKER)
            require(
                owner.get("houdini") == target and owner.get("preference_root"),
                "Cannot identify the registered Houdini preferences for isolated verification",
            )
            for path in (app, prefs, data):
                require(
                    not overlaps(path, existing_path) and not overlaps(path, owner["preference_root"]),
                    f"Isolated verification overlaps the registered installation or its preferences: {path}",
                )
        elif same_target:
            require(
                existing_path == app, f"This Houdini target is already installed at {existing_path}; update it in place"
            )
        if existing_path == app:
            marker = existing_path / "installation" / INSTALL_MARKER
            require(
                marker.exists() and read_json(marker).get("houdini") == target,
                f"Application directory belongs to a legacy or different target installer: {existing_path}",
            )
        else:
            require(
                not overlaps(app, existing_path)
                and not overlaps(data, existing_path)
                and not overlaps(prefs, existing_path),
                f"Path overlaps an installer-owned tree: {existing_path}",
            )
    if application_root and app.exists() and any(app.iterdir()):
        require((root / INSTALL_MARKER).exists(), f"Application directory is not owned by Cycles for Houdini: {app}")
    # A new package may expose several exact builds with one HDK API. Only
    # intersecting eligibility sets conflict in a shared preference folder.
    sdk = Path(os.environ.get("ProgramFiles", "")) / "Side Effects Software" / ("Houdini " + target)
    try:
        target_api_match = re.search(
            r"(?m)^#define HDK_API_VERSION\s+(\d+)",
            (sdk / "toolkit/include/UT/UT_HDKVersion.h").read_text(),
        )
        target_api = int(target_api_match[1]) if target_api_match else None
    except OSError:
        target_api = None
    planned_versions = package_eligible_versions(planned_enable or package_enable(target, target_api))
    # Shared preferences are also an ownership index for portable/isolated installs.
    for path in (prefs / "packages").glob("cycles_houdini*.json"):
        own_registration = path.name == identity["package_filename"]
        try:
            package = read_json(path)
        except (OSError, ValueError) as error:
            if own_registration:
                # The existing owned/unmanaged registration check below reports
                # the familiar actionable edit or replacement diagnostic.
                continue
            raise PackageError(f"Unparseable Cycles package registration: {path}") from error
        if path.name == PACKAGE_FILE:
            # Preserve the legacy ownership diagnostic before evaluating its gate.
            _legacy_owner(prefs)
        existing_versions = set() if own_registration else package_eligible_versions(package.get("enable"))
        try:
            if not own_registration and existing_versions:
                # Only the official build takes over the old official "dev" registration.
                migrating_legacy = (
                    build is None
                    and path.name == PACKAGE_FILE
                    and (_legacy_owner(prefs) or {}).get("manifest", {}).get("houdini") == target
                )
                if eligible_sets_overlap(existing_versions, planned_versions) and not migrating_legacy:
                    require(
                        False,
                        f"Another Cycles package in these preferences is eligible for the same Houdini build: {path.name}; remove it before installing",
                    )
            host = next(
                item["CYCLES_HOUDINI_HOST_ROOT"]
                for item in package.get("env", [])
                if "CYCLES_HOUDINI_HOST_ROOT" in item
            )
        except (ValueError, KeyError, StopIteration):
            continue
        payload = unlinked_path(host)
        if len(payload.parents) < 3 or payload.parent.parent.name != "versions":
            continue
        owned = payload.parents[2]
        marker = owned / INSTALL_MARKER
        if marker.exists() and owned != root:
            owner = read_json(marker)
            owner_app = unlinked_path(
                owner.get("application_root", owned.parent if owned.name == "installation" else owned)
            )
            require(
                not overlaps(app, owner_app) and not overlaps(data, owner_app),
                f"Path overlaps another target or legacy uninstaller tree: {owner_app}",
            )
            require(
                path.name != identity["package_filename"],
                f"This target registration is owned by another installation: {owned}",
            )


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def relative_name(name):
    require(isinstance(name, str) and name and "\\" not in name and ":" not in name, f"Invalid package path: {name!r}")
    value = PurePosixPath(name)
    require(
        not value.is_absolute() and all(p not in ("", ".", "..") for p in name.split("/")),
        f"Package path escapes its root: {name!r}",
    )
    require(
        all(not RESERVED.match(p) and p[-1] not in (" ", ".") and not any(ord(c) < 32 for c in p) for p in value.parts),
        f"Unsafe Windows package path: {name!r}",
    )
    return value


def inside(root, name):
    parts = relative_name(name).parts
    root = unlinked_path(root)
    path = root.joinpath(*parts)
    require(path.resolve().is_relative_to(root), f"Path escapes root: {name}")
    return unlinked_path(path)


def version_dir(root, package_id):
    """The folder of an installed payload: versions/<first 16 hex>; a payload installed before the short names keeps
    its versions/<64 hex> folder and is found there."""
    root = unlinked_path(root)
    legacy = inside(root, "versions/" + package_id)
    return legacy if legacy.exists() else inside(root, "versions/" + package_id[:VERSION_FOLDER_CHARS])


def notice_destination(component, target):
    """Where a notice text sits in the package: notices/<component>/<target>, or flat when that is too long."""
    name = "notices/" + component + "/" + target
    if len(name) <= NOTICE_PATH_LIMIT:
        return name
    tag = hashlib.sha256(target.encode("utf-8")).hexdigest()[:12]
    flat = "notices/" + component + "/" + tag + "-" + PurePosixPath(target).name
    return flat if len(flat) <= NOTICE_PATH_LIMIT else "notices/" + component + "/" + tag + ".txt"


def windows_path_length(path):
    return len(str(path).encode("utf-16-le", errors="surrogatepass")) // 2


def atomic_write_path(path, token):
    return path.with_name("." + path.name + "." + token + ".tmp")


def package_path_problem(names):
    """A plain sentence when a path in the package is longer than the budget, else None."""
    longest = max(names, key=windows_path_length)
    length = windows_path_length(longest)
    if length <= PACKAGE_PATH_BUDGET:
        return None
    return (
        f"The package path {longest} has {length} characters; at most {PACKAGE_PATH_BUDGET} fit, so that an "
        f"installation under an app folder of up to {APP_FOLDER_BUDGET} characters stays within "
        f"{INSTALLED_TOTAL_BUDGET}: give that file a shorter name."
    )


def install_path_problem(
    install_root,
    names,
    application_root=None,
    *,
    preference_root=None,
    package_filename=None,
    package_id=None,
    houdini=None,
):
    """Check final files and the paths needed for atomic activation and recovery."""
    root = PureWindowsPath(unlinked_path(install_root))
    app = PureWindowsPath(unlinked_path(application_root)) if application_root else root
    folder = version_dir(install_root, package_id).name if package_id else "0" * VERSION_FOLDER_CHARS
    staging = root / ".staging" / ("0" * VERSION_FOLDER_CHARS)
    version = root / "versions" / folder
    names = tuple(names)
    files = [directory / name for directory in (version, staging) for name in names]
    files.append(root / ".installation.lock")
    atomic = [
        root / INSTALL_MARKER,
        root / "state.json",
        root / "transactions" / ("0" * 32 + ".json"),
    ]
    files.extend(atomic)
    files.extend(atomic_write_path(path, "0" * 32) for path in atomic)
    if application_root:
        files.extend(
            app / name
            for name in (
                "tools/release-package.py",
                "tools/maintain-registration.ps1",
                "tools/refresh-registration.ps1",
                "Refresh Cycles registration.cmd",
                "houdini-python.txt",
                "install.log",
                f"uninstall-{houdini}.log",
                "LICENSE.txt",
                "THIRD_PARTY_NOTICES.md",
                "unins000.exe",
                "unins000.dat",
                "unins000.msg",
            )
        )
    groups = [(app, files)]
    if preference_root is not None:
        prefs = PureWindowsPath(unlinked_path(preference_root))
        registrations = [prefs / "packages" / name for name in (package_filename, PACKAGE_FILE)]
        paths = [prefs / ".installation.lock", *registrations]
        paths.extend(atomic_write_path(path, "0" * 32) for path in registrations)
        groups.append((prefs, paths))
    for shown, paths in groups:
        total = max(map(windows_path_length, paths))
        if total > WINDOWS_PATH_LIMIT:
            spare = total - WINDOWS_PATH_LIMIT
            units = "character" if spare == 1 else "characters"
            return (
                f"The folder {shown} is too long for Cycles for Houdini: its longest file would get a path of {total} "
                f"characters and Windows allows {WINDOWS_PATH_LIMIT}, so choose a folder at least {spare} {units} shorter."
            )
    return None


def unlinked_path(path):
    """Reject reparse points before resolution, including ancestors of a root."""
    path = Path(os.path.abspath(path))
    for parent in (path, *path.parents):
        require(
            not parent.is_symlink() and not (hasattr(parent, "is_junction") and parent.is_junction()),
            f"Reparse/symlink path is not allowed: {parent}",
        )
    return path


def atomic_bytes(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = atomic_write_path(path, uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_json(path, value):
    atomic_bytes(path, canonical(value))


def pe_imports(path):
    """Deployment includes normal and delay-loaded DLLs."""
    ordinary, delayed = pe_import_tables(path)
    return sorted(ordinary | delayed)


def pe_import_tables(path):
    """Inspect PE data directories without mapping the file as executable code."""
    with Path(path).open("rb") as stream:
        require(os.fstat(stream.fileno()).st_size > 0, f"Not a PE binary: {path}")
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return _read_import_tables(path, data)


def _read_import_tables(path, data):
    require(not data[:64].startswith(b"version https://git-lfs.github.com/"), f"Unfetched LFS object: {path}")
    require(len(data) >= 64 and data[:2] == b"MZ", f"Not a PE binary: {path}")
    u16 = lambda at: struct.unpack_from("<H", data, at)[0]
    u32 = lambda at: struct.unpack_from("<I", data, at)[0]
    try:
        offset = u32(0x3C)
        require(data[offset : offset + 4] == b"PE\0\0", f"Invalid PE signature: {path}")
        require(u16(offset + 4) == 0x8664, f"Expected Windows x64 binary: {path}")
        sections, optional_size = u16(offset + 6), u16(offset + 20)
        optional = offset + 24
        require(u16(optional) == 0x20B and optional_size >= 112, f"Expected PE32+: {path}")
        image_base = struct.unpack_from("<Q", data, optional + 24)[0]
        directories = min(u32(optional + 108), (optional_size - 112) // 8)
        section_table = optional + optional_size
        require(sections <= 256 and section_table + sections * 40 <= len(data), f"Invalid PE sections: {path}")
        mapping = []
        for n in range(sections):
            section = section_table + n * 40
            virtual_size, address, raw_size, raw_offset = struct.unpack_from("<4I", data, section + 8)
            require(raw_offset + raw_size <= len(data), f"Invalid PE raw section: {path}")
            mapping.append((address, max(virtual_size, raw_size), raw_offset, raw_size))

        def rva(address, size=1):
            if address < u32(optional + 60):
                require(address + size <= len(data), f"Invalid PE header RVA: {path}")
                return address
            for start, length, raw, available in mapping:
                if start <= address < start + length:
                    relative = address - start
                    require(relative + size <= available, f"PE RVA outside file-backed data: {path}")
                    return raw + relative
            raise PackageError(f"Unowned PE RVA in {path}")

        def name(address):
            start = rva(address)
            end = data.find(b"\0", start, min(len(data), start + 512))
            require(end != -1, f"Invalid imported DLL name in {path}")
            value = data[start:end].decode("ascii").lower()
            require(
                value and "/" not in value and "\\" not in value and ":" not in value,
                f"Unsafe imported DLL name in {path}",
            )
            return value

        ordinary, delayed = set(), set()
        for directory, stride, name_offset, result in ((1, 20, 12, ordinary), (13, 32, 4, delayed)):
            if directories <= directory:
                continue
            address, size = struct.unpack_from("<2I", data, optional + 112 + directory * 8)
            if not address or not size:
                continue
            require(size <= len(data), f"Unbounded PE import table: {path}")
            for n in range(size // stride):
                entry = rva(address + n * stride, stride)
                values = struct.unpack_from("<" + "I" * (stride // 4), data, entry)
                if not any(values):
                    break
                imported = u32(entry + name_offset)
                if directory == 13 and not values[0] & 1:
                    imported -= image_base
                result.add(name(imported))
            else:
                raise PackageError(f"Unterminated PE import table: {path}")
        return ordinary, delayed
    except (struct.error, UnicodeError, IndexError) as error:
        raise PackageError(f"Malformed PE binary {path}: {error}") from error


def _runtime_file(name):
    parts = PurePosixPath(name).parts
    return "__pycache__" not in parts and "backup" not in parts and not name.endswith((".pyc", ".pdb", ".lib", ".exp"))


REQUIRED_BRANDING = ("installer.ico", "installer-welcome.png", "installer-header.png", "cycles-houdini-logo.png")
# The Blender component (blender/ in the source, GPL-3.0-or-later; scripts that run inside Blender),
# staged to host/blender. It must ship complete and with its own licence text.
BLENDER_COMPONENT = ("LICENSE", "README.md", "worker_main.py", "asset_prefs_main.py", "export_main.py")


def runtime_overrides(files):
    """Plan transformed runtime bytes once, before hashing or archive emission."""
    if HYDRA_DESCRIPTOR not in files:
        return {}
    value = json.loads(Path(files[HYDRA_DESCRIPTOR]).read_bytes())
    # USD resolves Root relative to the plugInfo file's directory.
    for item in value["Plugins"]:
        item.update(Root="..", LibraryPath="hdCycles.dll", ResourcePath="../../../resources")
        item["Info"]["Types"]["HdCyclesPlugin"]["displayName"] = "Cycles"
    return {HYDRA_DESCRIPTOR: canonical(value)}


def validate_solaris_payload(features, files):
    enabled = str(features.get("WITH_CYCLES_HYDRA_RENDER_DELEGATE", "OFF")).upper() in ("ON", "TRUE", "1")
    plugins = [name for name in files if "/usd_plugins/" in name.lower() or Path(name).name.lower() == "hdcycles.dll"]
    if enabled:
        require(
            HYDRA_DESCRIPTOR in files and "host/dso/usd_plugins/hdCycles/hdCycles.dll" in files,
            "Solaris build is missing its delegate or plugin registration",
        )
    else:
        require(not plugins, "Native OBJ/SOP package contains Solaris plugin artifacts: " + ", ".join(plugins))


def sdk_dependency_record(source, hfs, policy):
    spec = importlib.util.spec_from_file_location(
        "ch_release_sdk_dependencies", source / "houdini/python3.13libs/cycles_houdini/core/sdk_dependencies.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.inspect_sdk(hfs, policy)


def payload_plan(build_root, source_root, community=False):
    build, source = Path(build_root).resolve(), Path(source_root).resolve()
    manifest_file = build / "build-manifest.json"
    manifest_bytes = manifest_file.read_bytes()
    manifest = json.loads(manifest_bytes)
    host = inside(build, manifest["resource_root"]).parent
    require(host.is_relative_to(build), "Host root must belong to the build")
    files, issues = {}, []
    policy = read_json(source / "source.lock.json")["sdk_dependencies"]["openimagedenoise"]
    provider = manifest.get("dependency_providers", {}).get("openimagedenoise")
    require(
        provider == sdk_dependency_record(source, Path(manifest["houdini_root"]), policy),
        "The built SDK OIDN provider differs from the pinned installation; rebuild",
    )
    frozen_provider = host / "resources/sdk-dependencies.json"
    require(
        read_json(frozen_provider) == dict(openimagedenoise=provider),
        "Frozen SDK dependency metadata differs from the build manifest",
    )

    def add(target, path, expected=None):
        relative_name(target)
        path = Path(path)
        require(path.is_file() and not path.is_symlink(), f"Missing/linked payload file: {path}")
        if expected is not None:
            require(file_hash(path) == expected, f"Build artifact hash mismatch: {path}")
        folded = target.casefold()
        require(all(k.casefold() != folded or k == target for k in files), f"Case-colliding payload: {target}")
        require(target not in files or file_hash(files[target]) == file_hash(path), f"Conflicting payload: {target}")
        files[target] = path

    for record in manifest["host_runtime"]:
        path = inside(build, record["path"])
        require(path.is_relative_to(host), f"Host runtime record outside selected host: {record['path']}")
        relative = path.relative_to(host).as_posix()
        if _runtime_file(relative):
            add("host/" + relative, path, record["sha256"])
    binaries = manifest["binary_artifacts"]
    for record in binaries:
        path = inside(build, record["path"])
        if path.is_relative_to(host):
            target = "host/" + path.relative_to(host).as_posix()
        elif path.name == "hdCycles.dll":
            target = "host/dso/usd_plugins/hdCycles/hdCycles.dll"
        else:
            target = "tools/" + path.name
        add(target, path, record["sha256"])
    for record in manifest["kernels"]:
        add("host/resources/lib/" + record["name"], inside(host / "resources/lib", record["name"]), record["sha256"])
    require(
        "host/bin/ch_worker.exe" in files and "host/dso/_cycles_houdini.dll" in files, "Missing native runtime binaries"
    )
    validate_solaris_payload(manifest["features"], files)
    require(
        "host/resources/sdk-dependencies.json" in files, "Frozen SDK provider policy is not in the runtime inventory"
    )
    missing_branding = [
        "host/config/branding/" + name for name in REQUIRED_BRANDING if "host/config/branding/" + name not in files
    ]
    require(
        not missing_branding,
        "Required branding missing from frozen runtime inventory; restage the host: " + ", ".join(missing_branding),
    )
    missing_blender = ["host/blender/" + name for name in BLENDER_COMPONENT if "host/blender/" + name not in files]
    require(
        not missing_blender,
        "Blender component missing from frozen runtime inventory; restage the host: " + ", ".join(missing_blender),
    )
    require(
        not any(
            Path(name).name.lower().startswith("openimagedenoise") and Path(name).suffix.lower() == ".dll"
            for name in files
        ),
        "SDK OIDN libraries must not be redistributed or shadowed in the host",
    )
    for record in manifest["integration_source"]:
        name = record["path"]
        if name.startswith(("include/", "src/", "houdini/", "patches/")) or name in (
            "CMakeLists.txt",
            "source.lock.json",
        ):
            path = inside(source, name)
            if not path.is_file() or file_hash(path) != record["sha256"]:
                issues.append(dict(code="build.source_changed", source=name))
    dependencies = source / "third_party/cycles/lib/windows_x64"
    runtime = {}
    for component in RUNTIME_COMPONENTS:
        directory = dependencies / component / "bin"
        require(directory.is_dir(), f"Missing dependency runtime directory: {directory}")
        for path in sorted(directory.glob("*.dll")):
            lower = path.name.lower()
            if (
                lower.endswith("_d.dll")
                or re.fullmatch(r"sycl\d+d\.dll", lower)
                or lower in ("ur_loaderd.dll", "ur_win_proxy_loaderd.dll", "xptifwd.dll")
                or re.fullmatch(r"ur_adapter_.*d\.dll", lower)
            ):
                continue
            with path.open("rb") as stream:
                pointer = stream.read(48).startswith(b"version https://git-lfs.github.com/")
            if pointer:
                issues.append(
                    dict(code="dependency.optional_lfs_stub", source=str(path.relative_to(source)), component=component)
                )
                continue
            require(
                lower not in runtime or file_hash(runtime[lower][0]) == file_hash(path),
                f"Conflicting dependency DLL name: {lower}",
            )
            runtime[lower] = (path, component)
            add("runtime/bin/" + path.name, path)
    hfs = Path(manifest["houdini_root"])
    host_dlls = {}
    for directory in (hfs / "bin", hfs / "python313", hfs / "python313/DLLs"):
        if directory.is_dir():
            for path in directory.glob("*.dll"):
                host_dlls[path.name.lower()] = path
    pe_files = [(target, path) for target, path in files.items() if path.suffix.lower() in (".dll", ".exe")]
    imports, missing = [], []
    required_components = {"integration", "cycles", "eigen", "houdini-sdk-openimagedenoise", "pybind11"}
    if str(manifest["features"].get("WITH_CYCLES_DEVICE_OPTIX")).upper() in ("ON", "TRUE", "1") or any(
        name.startswith("host/resources/lib/kernel_optix") for name in files
    ):
        required_components.add("optix-headers")
    if manifest["features"].get("WITH_DLSS") in ("ON", "TRUE", "1"):
        required_components.add("dlss-headers")
    # GPU programs made with NVIDIA's CUDA compiler (CUDA and OptiX kernels): the CUDA notice goes with them.
    if any(re.match(r"host/resources/lib/kernel_(sm_|compute_|optix)", name) for name in files):
        required_components.add("cuda")
    if str(manifest["features"].get("WITH_CYCLES_PATH_GUIDING")).upper() in ("ON", "TRUE", "1"):
        required_components.add("openpgl")
    linked = (build / "build.ninja").read_text(errors="replace") if (build / "build.ninja").exists() else ""
    for line in linked.splitlines():
        if line.strip().startswith(("LINK_LIBRARIES =", "INCLUDES =")):
            required_components.update(
                re.findall(r"third_party[\\/]cycles[\\/]lib[\\/]windows_x64[\\/]([^\\/\s]+)", line)
            )
    for target, path in pe_files:
        for name in pe_imports(path):
            entry = dict(consumer=target, name=name)
            if name in runtime:
                entry.update(provider="bundled", component=runtime[name][1])
                required_components.add(runtime[name][1])
                if name in host_dlls and file_hash(runtime[name][0]) != file_hash(host_dlls[name]):
                    entry.update(
                        provider="conflicting-provider",
                        reason="Bundled DLL conflicts with the pinned Houdini SDK",
                        houdini_sha256=file_hash(host_dlls[name]),
                        bundled_sha256=file_hash(runtime[name][0]),
                    )
                    missing.append(entry)
            elif name in host_dlls:
                entry.update(
                    provider="houdini-sdk" if name.startswith("openimagedenoise") else "houdini",
                    sha256=file_hash(host_dlls[name]),
                )
                if name.startswith("openimagedenoise"):
                    entry.update(
                        component="houdini-sdk-openimagedenoise", version=provider["version"], redistributed=False
                    )
            elif name.startswith(("api-ms-win-", "ext-ms-win-")) or name in SYSTEM_DLLS:
                entry.update(provider="windows")
            elif VC_DLLS.match(name):
                entry.update(provider="msvc-redistributable")
            elif name in DRIVER_DLLS:
                entry.update(
                    provider="gpu-runtime-prerequisite",
                    optional=False,
                    prerequisite="AMD HIP 7 runtime" if name == "amdhip64_7.dll" else "Matching GPU driver/runtime",
                )
            else:
                entry.update(provider="unresolved")
                missing.append(entry)
            imports.append(entry)
    required_components.update(value[1] for value in runtime.values())
    for name, path in (
        ("LICENSE", source / "LICENSE"),
        ("THIRD_PARTY_NOTICES.md", source / "THIRD_PARTY_NOTICES.md"),
        (BUILD_PROVENANCE, manifest_file),
        ("provenance/source.lock.json", source / "source.lock.json"),
        ("tools/release-package.py", Path(__file__)),
        ("tools/maintain-registration.ps1", source / "scripts/maintain-registration.ps1"),
        ("tools/refresh-registration.ps1", source / "scripts/refresh-registration.ps1"),
        ("tools/Refresh Cycles registration.cmd", source / "launchers/Refresh Cycles registration.cmd"),
    ):
        add(name, path)
    for record in manifest["applied_patches"]:
        add("provenance/" + record["path"], inside(source, record["path"]), record["sha256"])
    policy_error = None
    if community:
        policy, policy_error = community_policy_files(source, manifest)
    else:
        policy = fork_policy_files(source, manifest)
    for name, path in policy.items():
        add("provenance/" + name, path)
    # Include every staged Python/HDA/menu/resource and copied dependency. Native
    # binaries alone cannot certify a host whose UI or capture implementation changed.
    identity = {key: manifest[key] for key in ("actual_revisions", "features", "host_sdk", "applied_patches")}
    identity["source_fingerprint"] = manifest.get("source_fingerprint")
    identity["dependency_providers"] = manifest["dependency_providers"]
    replacements = runtime_overrides(files)
    identity["runtime_files"] = [
        dict(path=name, sha256=digest(replacements[name]) if name in replacements else file_hash(path))
        for name, path in sorted(files.items())
        if name.startswith(("host/", "runtime/")) or path.suffix.lower() in (".dll", ".exe")
    ]
    fingerprint = digest(canonical(identity))
    return dict(
        build=build,
        source=source,
        files=files,
        manifest=manifest,
        build_manifest_sha256=digest(manifest_bytes),
        issues=issues,
        imports=imports,
        missing=missing,
        components=sorted(required_components),
        runtime_fingerprint=fingerprint,
        runtime_overrides=replacements,
        policy_error=policy_error,
    )


def fork_policy_files(source, manifest):
    """Ship the decisions that authorize the exact built fork, including rebase grants."""
    lock = read_json(source / "source.lock.json")["cycles"]
    # Packages ship the decisions for the exact authoring commits: private engine route only.
    require(
        manifest.get("engine_route") != "public",
        "Release packages are built only from the private engine route (third_party/cycles at cycles.authoring_tip, "
        "scripts/bootstrap.ps1 -EngineSource Private). This build used the public route (upstream Cycles plus "
        "patches/), which is for development builds and tests; rebuild from the private route before packaging",
    )
    require(
        manifest["actual_revisions"].get("cycles") == lock.get("authoring_tip", lock["commit"])
        and manifest["applied_patches"]
        == [dict(path=name, sha256=lock["patch_sha256"][name]) for name in lock["patches"]],
        "Fork policy differs from the built engine or patch stack; rebuild before packaging",
    )
    return _review_record_files(source, lock)


def _review_record_files(source, lock):
    policy = runpy.run_path(str(Path(__file__).with_name("patch-triage.py")))["validate"](source)
    names = {"patches/triage.json", "docs/decisions/0005-cycles-fork-policy.md"}
    names.update(policy["approved_features"].values())
    names.update(record["grant"] for record in policy["reviewed_exceptions"].values() if "grant" in record)
    if lock.get("release_rebase_approval"):
        name = lock["release_rebase_approval"]
        receipt = read_json(inside(source, name))
        names.add(name)
        names.add(receipt["decision"])
        names.add(lock["candidate"]["provenance"])
        names.update(
            str(PurePosixPath(name).parent / record) for record in ("replay.json", "previous-source.lock.json")
        )
    return {name: inside(source, name) for name in sorted(names)}


def community_policy_files(source, manifest):
    """The fork_policy_files check for a community build, which may use the public engine route.

    The private route keeps its exact commit check. The public route cannot have
    the private commit ids, so the nearest check is the engine's Git tree
    (cycles.authoring_tree) plus the exact patch list and hashes. The review
    records (patches/triage.json and the decisions it names) are shipped when they
    still cover the patch series; patches added by the community builder are not
    reviewed there, and the returned message says so instead of failing.
    """
    lock = read_json(source / "source.lock.json")["cycles"]
    # The build records the SHA-256 of each patch file as it is on disk; a Windows checkout with
    # core.autocrlf=true has CRLF copies, while the lock records the LF bytes Git stores (as
    # engine-fork.py compares them). Check both: same files as built, same series as locked.
    records = manifest["applied_patches"]
    require(
        [record["path"] for record in records] == list(lock["patches"]),
        "The built patch series differs from source.lock.json; rebuild before packaging",
    )
    for record in records:
        data = inside(source, record["path"]).read_bytes()
        require(
            digest(data) == record["sha256"]
            and digest(data.replace(b"\r\n", b"\n")) == lock["patch_sha256"][record["path"]],
            "Patch file differs from the build or from source.lock.json; rebuild before packaging: " + record["path"],
        )
    if manifest.get("engine_route") == "public":
        require(
            manifest.get("engine_tree") and manifest.get("engine_tree") == lock.get("authoring_tree"),
            "The built engine tree differs from cycles.authoring_tree; rebuild before packaging",
        )
    else:
        require(
            manifest["actual_revisions"].get("cycles") == lock.get("authoring_tip", lock["commit"]),
            "Fork policy differs from the built engine; rebuild before packaging",
        )
    try:
        return _review_record_files(source, lock), None
    except (PackageError, RuntimeError, KeyError, OSError, ValueError) as error:
        return {}, "Patch review records do not cover this patch series: " + str(error)


def audit(plan):
    source = plan["source"]
    catalog_path = source / "packaging/notices/index.json"
    catalog = read_json(catalog_path) if catalog_path.is_file() else dict(components={})
    licenses, license_files, notice_names = [], {}, {}
    required = set(plan["components"])
    pending = list(required)
    while pending:
        for dependency in catalog.get("components", {}).get(pending.pop(), {}).get("requires", []):
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    for component in sorted(required):
        record = catalog.get("components", {}).get(component)
        entry = dict(component=component, result="blocked", reasons=[])
        if not record:
            entry["reasons"].append("No pinned notice inventory")
        else:
            entry.update(version=record["version"], license=record.get("license"), provenance=record.get("provenance"))
            if record.get("coverage") != "complete":
                entry["reasons"].append(record.get("reason", "Notice coverage requires review"))
            for notice in record.get("files", []):
                path = inside(source, notice["path"])
                if not path.is_file() or file_hash(path) != notice["sha256"]:
                    entry["reasons"].append("Missing/changed notice: " + notice["path"])
                else:
                    target = notice.get("target", path.name)
                    relative_name(target)
                    key = notice_destination(component, target)
                    require(
                        key not in license_files or file_hash(license_files[key]) == notice["sha256"],
                        "Conflicting notice destination: " + key,
                    )
                    license_files[key] = path
                    notice_names[key] = dict(component=component, original="notices/" + component + "/" + target)
            if not record.get("files"):
                entry["reasons"].append("No notice text")
            if component == "optix-headers":
                if record["version"] != plan["manifest"]["actual_revisions"].get("optix"):
                    entry["reasons"].append("OptiX notices do not match the SDK revision in the build")
                for target in OPTIX_NOTICE_FILES:
                    if notice_destination("optix-headers", target) not in license_files:
                        entry["reasons"].append("Required OptiX notice unavailable: " + target)
            if component == "openpgl":
                if record.get("provenance", {}).get("revision") != plan["manifest"]["actual_revisions"].get(
                    "dependencies"
                ):
                    entry["reasons"].append("OpenPGL notices do not match the dependency revision in the build")
                for target in OPENPGL_NOTICE_FILES:
                    if notice_destination("openpgl", target) not in license_files:
                        entry["reasons"].append("Required OpenPGL notice unavailable: " + target)
        if not entry["reasons"]:
            entry["result"] = "passed"
        licenses.append(entry)
    source_issues = [x for x in plan["issues"] if x["code"] == "build.source_changed"]
    gates = dict(
        build_sources="passed" if not source_issues else "blocked",
        dependencies="passed" if not plan["missing"] else "blocked",
        notices="passed" if all(x["result"] == "passed" for x in licenses) else "blocked",
    )
    result = dict(
        schema_version=SCHEMA,
        result="passed" if all(v == "passed" for v in gates.values()) else "blocked",
        runtime_fingerprint=plan["runtime_fingerprint"],
        gates=gates,
        issues=plan["issues"],
        imports=plan["imports"],
        unresolved_imports=plan["missing"],
        licenses=licenses,
        houdini=plan["manifest"]["host_sdk"],
        runtime_components=plan["components"],
        notice_components=sorted(required),
        # Where each notice text is stored in the package and its original path (only deep ones are stored flat).
        notice_index={key: notice_names[key] for key in sorted(notice_names)},
        dependency_providers=plan["manifest"].get("dependency_providers"),
        external_prerequisites=[
            "Matching Houdini installation",
            "Windows x64",
            "Microsoft Visual C++ x64 runtime",
            "A supported GPU driver when GPU devices are used",
        ],
        scope="File closure and pinned notice coverage; runtime certification is a separate release gate.",
    )
    return result, license_files


def portable_build_manifest(raw):
    """Project audit-only builder details while binding the original frozen bytes."""
    manifest = json.loads(raw)
    require("public_provenance" not in manifest, "Expected the original frozen build manifest")
    manifest.pop("houdini_root", None)
    manifest.get("compiler", {}).pop("path", None)
    manifest.get("ocio", {}).pop("config", None)
    for gpu in manifest.get("build_host", {}).get("gpus", ()):
        gpu.pop("uuid", None)
        gpu.pop("pci_bus_id", None)
    manifest["public_provenance"] = {"schema_version": 1, PARENT_MANIFEST: digest(raw)}
    return canonical(manifest)


def public_sdk_root(files, houdini, reviewed_roots=None):
    require(ASSET_SEAL in files, "Public package requires the frozen asset seal")
    root = json.loads(files[ASSET_SEAL])["recipe"]["houdini_root"]
    path = PureWindowsPath(root)
    require(
        path.is_absolute()
        and re.fullmatch(r"[A-Za-z]:", path.drive)
        and ".." not in path.parts
        and not {"users", "documents and settings"}.intersection(part.casefold() for part in path.parts),
        "Public asset seal requires a nonpersonal local SDK root",
    )
    standard = PureWindowsPath(path.anchor, "Program Files", "Side Effects Software", "Houdini " + houdini)
    require(
        reviewed_roots is None or path == standard or any(path == PureWindowsPath(item) for item in reviewed_roots),
        "Public asset seal SDK root must use Program Files or an explicitly reviewed --public-sdk-root",
    )
    return root


def verify_public_json(name, value, sdk_root, fields=()):
    """The asset seal pathway is the only intentionally published absolute path."""
    require(
        name != BUILD_PROVENANCE or fields not in PRIVATE_BUILD_FIELDS,
        f"Public build manifest exposes a private builder field at {'.'.join(fields)}",
    )
    if name == ASSET_SEAL and fields == ("recipe", "houdini_root") and value == sdk_root:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            verify_public_json(name, key, sdk_root, fields + ("<key>",))
            verify_public_json(name, item, sdk_root, fields + (key,))
    elif isinstance(value, list):
        for item in value:
            verify_public_json(name, item, sdk_root, fields + ("[]",))
    elif isinstance(value, str):
        require(
            not re.search(r"""(?:^|[\s"'(=])(?:[A-Za-z]:[\\/]|\\\\|//|file://|/(?:home|Users|mnt|opt|tmp)/)""", value),
            f"Public package contains a builder path in {name} at {'.'.join(fields)}",
        )


def public_payload_binding(manifest):
    payload = {key: value for key, value in manifest.items() if key not in ("package_id", PUBLIC_BINDING)}
    payload["files"] = [record for record in manifest["files"] if record["path"] != CERTIFICATION]
    return digest(canonical(payload))


def public_certificate_fields(manifest, build):
    provenance = json.loads(build)["public_provenance"]
    parent = provenance[PARENT_MANIFEST]
    require(
        set(provenance) == {"schema_version", PARENT_MANIFEST}
        and provenance["schema_version"] == 1
        and isinstance(parent, str)
        and re.fullmatch("[0-9a-f]{64}", parent),
        "Invalid public build provenance parent",
    )
    return dict(
        result="passed",
        runtime_fingerprint=manifest["runtime_fingerprint"],
        public_payload_sha256=manifest[PUBLIC_BINDING],
        parent_build_manifest_sha256=parent,
    )


def archive_contents(plan, report, notices, purpose, public_sdk_roots=(), build=None, build_record=None):
    replacements = plan["runtime_overrides"]
    files = {
        name: replacements[name] if name in replacements else path.read_bytes() for name, path in plan["files"].items()
    }
    files.update({name: path.read_bytes() for name, path in notices.items()})
    if notices:
        # Every stored notice with its original path, so that nothing about what is included changes, only where a
        # few texts sit.
        known = report.get("notice_index", {})
        files[NOTICE_INDEX] = canonical(
            dict(
                schema_version=1,
                about="Every licence or notice text in this folder: where it is stored and its path in the "
                "component's own source. Texts whose path would be longer than "
                + str(NOTICE_PATH_LIMIT)
                + " characters "
                "are stored flat in their component's folder under a name made of 12 hex of the original path and the "
                "file name.",
                notices={
                    name: known.get(name, dict(component=PurePosixPath(name).parts[1], original=name))
                    for name in sorted(notices)
                },
            )
        )
    files["provenance/dependency-audit.json"] = canonical(report)
    if purpose == "public":
        sdk_root = public_sdk_root(files, plan["manifest"]["host_sdk"]["houdini"], public_sdk_roots)
        require(
            PureWindowsPath(sdk_root) == PureWindowsPath(plan["manifest"]["houdini_root"]),
            "Public asset seal SDK root differs from the frozen build manifest",
        )
        require(
            digest(files[BUILD_PROVENANCE]) == plan["build_manifest_sha256"],
            "Build manifest changed after planning the public archive",
        )
        files[BUILD_PROVENANCE] = portable_build_manifest(files[BUILD_PROVENANCE])
        for name, content in files.items():
            if name.endswith(".json"):
                verify_public_json(name, json.loads(content), sdk_root)
    manifest = dict(
        schema_version=SCHEMA,
        product="Cycles for Houdini",
        purpose=purpose,
        runtime_fingerprint=plan["runtime_fingerprint"],
        houdini=plan["manifest"]["host_sdk"]["houdini"],
        platform="windows-x64",
        files=file_records(files),
        audit=report["gates"],
        certification="passed" if purpose == "public" else "internal candidate; not a public release",
    )
    if build is not None:
        require(purpose == "internal", "A community build cannot be packaged as a certified public release")
        manifest["build_identity"] = build_identity(build)
        manifest["certification"] = "community build; not certified by the Cycles for Houdini project"
        require(build_record is None or isinstance(build_record, dict), "The build record must be a JSON object")
        # The caller's record can never make a community build official.
        manifest["build_record"] = dict(
            build_record or {}, official=False, fork_policy=plan.get("policy_error") or "shipped"
        )
    if purpose == "public":
        manifest[PUBLIC_BINDING] = public_payload_binding(manifest)
        verify_public_json(MANIFEST, manifest, sdk_root)
    return files, manifest


def file_records(files):
    return [dict(path=name, bytes=len(value), sha256=digest(value)) for name, value in sorted(files.items())]


def make_archive(
    build_root,
    source_root,
    output,
    purpose="internal",
    certification=None,
    public_sdk_roots=(),
    build=None,
    build_record=None,
):
    build = build_identity(build)
    plan = payload_plan(build_root, source_root, community=build is not None)
    report, notices = audit(plan)
    require(report["gates"]["build_sources"] == "passed", "Compiled runtime sources changed; rebuild before packaging")
    require(report["gates"]["dependencies"] == "passed", "Unresolved runtime DLL dependencies; inspect audit report")
    if "optix-headers" in report.get("notice_components", ()):
        require(
            all("notices/optix-headers/" + name in notices for name in OPTIX_NOTICE_FILES),
            "OptiX-enabled packages must contain the verified SDK and header notices",
        )
    if "openpgl" in report.get("notice_components", ()):
        require(
            any(entry["component"] == "openpgl" and entry["result"] == "passed" for entry in report["licenses"]),
            "Path-guiding packages must contain the verified OpenPGL dependency notices",
        )
    if purpose == "public":
        require(report["result"] == "passed", "Public release requires complete pinned notice coverage")
    certificate = read_json(certification) if certification else None
    if purpose == "public":
        require(
            isinstance(certificate, dict)
            and certificate.get("result") == "passed"
            and certificate.get("runtime_fingerprint") == plan["runtime_fingerprint"],
            "Public release requires passing certification for this exact runtime fingerprint",
        )
    files, manifest = archive_contents(plan, report, notices, purpose, public_sdk_roots, build, build_record)
    if purpose == "public":
        require(
            certificate == public_certificate_fields(manifest, files[BUILD_PROVENANCE]),
            "Public certification does not bind this exact payload and its original build manifest",
        )
    if certification:
        files[CERTIFICATION] = canonical(certificate)
        manifest["files"] = file_records(files)
    manifest["package_id"] = digest(canonical(manifest))
    files[MANIFEST] = canonical(manifest)
    problem = package_path_problem(list(files))
    require(problem is None, problem)
    output = Path(output).resolve()
    require(not output.exists(), f"Refusing to overwrite archive: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name("." + output.name + "." + uuid.uuid4().hex)
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_STORED) as archive:
            for name, value in sorted(files.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, value)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return dict(
        result="passed",
        archive=str(output),
        sha256=file_hash(output),
        bytes=output.stat().st_size,
        package_id=manifest["package_id"],
        purpose=purpose,
        runtime_fingerprint=plan["runtime_fingerprint"],
    )


def verify_public_binding(manifest, read_member):
    names = {record["path"] for record in manifest["files"]}
    if PUBLIC_BINDING not in manifest:
        # Legacy public and internal archives retain their original manifest path.
        if manifest.get("purpose") == "public" and BUILD_PROVENANCE in names:
            require(
                "public_provenance" not in json.loads(read_member(BUILD_PROVENANCE)),
                "Portable build provenance is missing its public payload binding",
            )
        return
    require(
        manifest["purpose"] == "public" and {BUILD_PROVENANCE, CERTIFICATION, ASSET_SEAL} <= names,
        "Incomplete portable public release",
    )
    verify_public_release_gates(manifest)
    require(
        set(manifest["audit"]) == {"build_sources", "dependencies", "notices"},
        "Incomplete portable public release gates",
    )
    require(manifest[PUBLIC_BINDING] == public_payload_binding(manifest), "Public payload binding mismatch")
    build = read_member(BUILD_PROVENANCE)
    certificate = json.loads(read_member(CERTIFICATION))
    require(
        certificate == public_certificate_fields(manifest, build),
        "Public certification does not bind this exact payload and its original build manifest",
    )
    # The verified certificate attests the exact SDK seal bytes, including any reviewed
    # alternate root. Verification never needs that path to exist on this machine.
    sdk_root = public_sdk_root({ASSET_SEAL: read_member(ASSET_SEAL)}, manifest["houdini"])
    verify_public_json(MANIFEST, manifest, sdk_root)
    for name in names:
        if name.endswith(".json"):
            verify_public_json(name, json.loads(read_member(name)), sdk_root)


def matches_build_provenance(manifest, archive_bytes, original_bytes):
    if PUBLIC_BINDING not in manifest:
        return archive_bytes == original_bytes
    return archive_bytes == portable_build_manifest(original_bytes)


def verify_public_release_gates(manifest):
    require(
        manifest.get("certification") == "passed"
        and manifest.get("audit")
        and all(value == "passed" for value in manifest["audit"].values()),
        "Public package gates did not pass",
    )


def _verify_archive(archive):
    names = [item.filename for item in archive.infolist()]
    require(len(names) == len(set(x.casefold() for x in names)), "Duplicate/case-colliding archive entries")
    for item in archive.infolist():
        relative_name(item.filename)
        require(
            not item.is_dir() and (item.external_attr >> 16) & 0o170000 != 0o120000, "Linked/directory archive entry"
        )
        require(item.file_size <= 2 * 1024**3, "Unbounded package entry")
    require(
        len(names) <= 100000 and sum(item.file_size for item in archive.infolist()) <= 8 * 1024**3,
        "Unbounded package size/file count",
    )
    require(MANIFEST in names, "Missing release manifest")
    require(archive.getinfo(MANIFEST).file_size <= 16 * 1024**2, "Unbounded release manifest")
    manifest = json.loads(archive.read(MANIFEST))
    require(
        manifest.get("schema_version") == SCHEMA and manifest.get("platform") == "windows-x64",
        "Unsupported release manifest",
    )
    require(manifest.get("purpose") in ("public", "internal"), "Unknown package purpose")
    if manifest["purpose"] == "public":
        verify_public_release_gates(manifest)
    if "build_identity" in manifest:
        require(manifest["purpose"] == "internal", "A community build cannot be a certified public release")
        build_identity(manifest["build_identity"])
    identity = dict(manifest)
    package_id = identity.pop("package_id")
    require(
        re.fullmatch("[0-9a-f]{64}", package_id) and digest(canonical(identity)) == package_id,
        "Release identity mismatch",
    )
    records = manifest["files"]
    require(len(records) == len({x["path"].casefold() for x in records}), "Duplicate manifest paths")
    require(set(names) == {x["path"] for x in records} | {MANIFEST}, "Archive entries differ from manifest")
    for entry in records:
        relative_name(entry["path"])
        value = archive.read(entry["path"])
        require(
            len(value) == entry["bytes"] and digest(value) == entry["sha256"], "Payload hash mismatch: " + entry["path"]
        )
    verify_public_binding(manifest, archive.read)
    return manifest


def verify_archive(path, expected_sha256=None):
    path = unlinked_path(path)
    with path.open("rb") as stream:
        if expected_sha256:
            require(
                hashlib.file_digest(stream, "sha256").hexdigest() == expected_sha256.lower(),
                "Archive checksum differs from the expected publisher checksum",
            )
            stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            return _verify_archive(archive)


def archive_sdk_provider(path):
    with zipfile.ZipFile(path) as archive:
        try:
            value = archive.read("host/resources/sdk-dependencies.json")
        except KeyError:
            return None  # Retained internal test fixtures predate the frozen provider.
    return json.loads(value)["openimagedenoise"]


def _owned_root(root, create=False):
    root = unlinked_path(root)
    require(root != Path(root.anchor) and root != Path.home().resolve(), "Choose a dedicated installation directory")
    require(
        not root.is_symlink() and not (hasattr(root, "is_junction") and root.is_junction()), "Linked installation root"
    )
    marker = root / INSTALL_MARKER
    if not marker.exists():
        require(
            create and (not root.exists() or not any(root.iterdir())),
            "Installation directory is not owned by Cycles for Houdini",
        )
        root.mkdir(parents=True, exist_ok=True)
        atomic_json(marker, dict(schema_version=SCHEMA, product="Cycles for Houdini", installation_id=uuid.uuid4().hex))
    require(read_json(marker).get("product") == "Cycles for Houdini", "Invalid installation ownership marker")
    return root


@contextmanager
def installation_lock(root):
    """OS-owned lock releases on process termination; its file can safely persist."""
    path = inside(root, ".installation.lock")
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise PackageError("Another installation operation holds the installation lock") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _pending(root):
    directory = root / "transactions"
    return [
        path
        for path in sorted(directory.glob("*.json"))
        if read_json(path).get("state") not in ("committed", "rolled_back")
    ]


def _config_bytes(path):
    path = unlinked_path(path)
    return path.read_bytes() if path.exists() else None


def _encoded(value):
    return base64.b64encode(value).decode() if value is not None else None


def _decoded(value):
    return base64.b64decode(value, validate=True) if value is not None else None


def _activate(
    root,
    package_path,
    new_value,
    new_state,
    operation,
    extra_changes=(),
    expected_previous=None,
    defer_commit=False,
    setup_token=None,
):
    require(not _pending(root), "An interrupted transaction needs recover before another change")
    old_value = _config_bytes(package_path)
    require(old_value == expected_previous, "Configuration changed during transaction preparation; preserving it")
    state_path = root / "state.json"
    before_state_bytes = _config_bytes(state_path)
    before_state = json.loads(before_state_bytes) if before_state_bytes is not None else None
    marker = read_json(root / INSTALL_MARKER)
    transaction = dict(
        schema_version=SCHEMA,
        operation=operation,
        state="prepared",
        setup_owned=defer_commit,
        setup_token=setup_token,
        installation_id=marker["installation_id"],
        package_path=str(package_path),
        preference_root=str(package_path.parent.parent),
        package_filename=package_path.name,
        houdini=new_state.get("houdini"),
        owned_sha256=digest(new_value) if new_value is not None else None,
        previous=_encoded(old_value),
        intended=_encoded(new_value),
        previous_state=before_state,
        intended_state=new_state,
        previous_state_bytes=_encoded(before_state_bytes),
        intended_state_bytes=_encoded(canonical(new_state)),
    )
    # A community build's journal names its build identity, so commit, abort and
    # recover check its own package file name. Official journals are unchanged.
    build = new_state.get("build_identity") or (before_state or {}).get("build_identity")
    if build is not None:
        transaction["build_identity"] = build_identity(build)
    transaction["extra_changes"] = [
        dict(path=str(path), previous=_encoded(_config_bytes(path)), intended=_encoded(value))
        for path, value in extra_changes
    ]
    for change in transaction["extra_changes"]:
        destination = Path(change["path"])
        if destination.name == "state.json":
            legacy_state = json.loads(_decoded(change["previous"]))
            legacy_version = version_dir(destination.parent, legacy_state["active"])
            transaction["legacy_owner"] = dict(
                root=str(destination.parent),
                installation_id=read_json(destination.parent / INSTALL_MARKER)["installation_id"],
                manifest_sha256=file_hash(legacy_version / MANIFEST),
            )
    path = root / "transactions" / (uuid.uuid4().hex + ".json")
    atomic_json(path, transaction)
    if new_value is None:
        if package_path.exists():
            package_path.unlink()
    else:
        atomic_bytes(package_path, new_value)
    for change in transaction["extra_changes"]:
        destination, content = unlinked_path(change["path"]), _decoded(change["intended"])
        if content is None:
            if destination.exists():
                destination.unlink()
        else:
            atomic_bytes(destination, content)
    atomic_json(state_path, new_state)
    transaction["state"] = "setup_pending" if defer_commit else "committed"
    atomic_json(path, transaction)


def _legacy_owner(preference_root):
    path = inside(preference_root, "packages/" + PACKAGE_FILE)
    value = _config_bytes(path)
    if value is None:
        return None
    package = json.loads(value)
    if package.get("enable") is False:
        # A disabled unmanaged registration is harmless. A registration still
        # pointing into an owned installation must pass the same receipt check:
        # an artist's disable/edit must never be mistaken for permission to
        # bypass that owner's original backup and old uninstaller state.
        try:
            disabled_host = next(
                item["CYCLES_HOUDINI_HOST_ROOT"]
                for item in package.get("env", [])
                if "CYCLES_HOUDINI_HOST_ROOT" in item
            )
            disabled_root = unlinked_path(disabled_host).parents[2]
        except (IndexError, KeyError, StopIteration):
            return None
        if not (disabled_root / INSTALL_MARKER).exists():
            return None
    try:
        host = next(item["CYCLES_HOUDINI_HOST_ROOT"] for item in package["env"] if "CYCLES_HOUDINI_HOST_ROOT" in item)
        root = unlinked_path(host).parents[2]
        state = read_json(root / "state.json")
        require(
            state.get("active")
            and state.get("package_path") == str(path)
            and state.get("package_sha256") == digest(value),
            "Legacy package ownership is ambiguous; preserving it",
        )
        _owned_root(root)
        manifest = verify_installed(version_dir(root, state["active"]))
        require(
            unlinked_path(host) == version_dir(root, state["active"]) / "host",
            "Legacy host path differs from its receipt",
        )
        require(
            package.get("enable")
            in (
                f"houdini_version == '{manifest['houdini']}'",
                f"houdini_version == '{'.'.join(manifest['houdini'].split('.')[:2])}'",
            ),
            "Legacy host enable condition differs from its manifest",
        )
        marker = read_json(root / INSTALL_MARKER)
        return dict(
            root=root,
            application_root=unlinked_path(marker.get("application_root", root.parent)),
            state=state,
            path=path,
            manifest=manifest,
        )
    except (KeyError, IndexError, StopIteration, OSError) as error:
        raise PackageError("Legacy package ownership is ambiguous; preserving it") from error


def registration_status(
    install_root, preference_root, data_root, houdini, application_root=None, archive=None, build_id=None
):
    """Read-only Setup consent preflight. Install repeats all ownership checks.

    A retained inactive receipt does not own the restored artist registration.
    Never offer replacement consent for an active edited or foreign receipt.
    """
    root, prefs, data = map(unlinked_path, (install_root, preference_root, data_root))
    planned = None
    build = None
    archived = None
    if archive:
        archived = verify_archive(archive)
        require(archived["houdini"] == houdini, "Registration preflight target differs from package")
        build = archived.get("build_identity")
        provider = archive_sdk_provider(archive)
        if provider:
            planned = package_enable(houdini, provider.get("hdk_api"), provider=provider)
    require(
        build_id is None or (build or {}).get("build_id") == build_id,
        "Registration preflight build identity differs from the package (pass --archive)",
    )
    identity = target_identity(houdini, build)
    names = [entry["path"] for entry in archived["files"]] + [MANIFEST] if archived else []
    problem = install_path_problem(
        root,
        names,
        application_root,
        preference_root=prefs,
        package_filename=identity["package_filename"],
        package_id=archived["package_id"] if archived else None,
        houdini=houdini,
    )
    require(problem is None, problem)
    validate_install_paths(root, prefs, data, houdini, application_root, planned_enable=planned, build=build)
    legacy = _legacy_owner(prefs)
    require(
        build is None or not legacy or legacy["manifest"]["houdini"] != houdini,
        "An older official Cycles for Houdini is registered for this Houdini; uninstall it before installing a community build",
    )
    state = {}
    if root.exists() and any(root.iterdir()):
        _owned_root(root)
        marker = read_json(root / INSTALL_MARKER)
        require(marker.get("houdini", houdini) == houdini, "Installation belongs to another Houdini target")
        require(
            marker.get("package_filename", identity["package_filename"]) == identity["package_filename"],
            "This folder belongs to another Cycles for Houdini build; choose another folder",
        )
        require(
            marker.get("preference_root", str(prefs)) == str(prefs),
            "Installation belongs to another Houdini preference root",
        )
        require(not _pending(root), "An interrupted transaction needs recover before installation")
        state = read_json(root / "state.json") if (root / "state.json").exists() else {}
    path = inside(prefs, "packages/" + identity["package_filename"])
    value = _config_bytes(path)
    if state.get("active"):
        require(
            state.get("houdini") == houdini,
            "Installation belongs to a legacy or different target; choose a dedicated directory",
        )
        require(_package_path(state) == path, "Installation belongs to another Houdini preference root")
        require(
            value is not None and digest(value) == state["package_sha256"],
            "Active package configuration was changed externally",
        )
        ownership = "owned"
    else:
        ownership = "unmanaged" if value is not None else "absent"
    return dict(result="passed", ownership=ownership, replace_existing_required=ownership == "unmanaged")


def install_components(state, blender_bridge=None, asset_browser=None):
    """A per-install choice; legacy receipts and first installs keep the bridge.

    The asset browser is a child of the bridge: it defaults to off for legacy
    receipts and first installs without an explicit choice, follows the stored
    choice on repair, and is switched off whenever the bridge is off. An
    explicit request to enable it while the bridge ends up disabled is refused
    rather than silently dropped.
    """
    previous = state.get("components", {"blender_bridge": True})
    require(
        isinstance(previous, dict) and type(previous.get("blender_bridge")) is bool,
        "Invalid stored Blender bridge component selection",
    )
    require(
        type(previous.get("asset_browser", False)) is bool,
        "Invalid stored asset browser component selection",
    )
    require(blender_bridge is None or type(blender_bridge) is bool, "Invalid Blender bridge component selection")
    require(asset_browser is None or type(asset_browser) is bool, "Invalid asset browser component selection")
    bridge = previous["blender_bridge"] if blender_bridge is None else blender_bridge
    require(
        not (asset_browser and not bridge),
        "The asset browser requires the Blender bridge; enable the bridge or disable the asset browser",
    )
    browser = previous.get("asset_browser", False) if asset_browser is None else asset_browser
    return dict(blender_bridge=bridge, asset_browser=browser and bridge)


ASSET_BROWSER_ADDON = "addons/asset_browser"


def package_hpath(host, components):
    """Houdini search path: the host, plus optional add-ons the install selected."""
    hpath = [host.as_posix()]
    if components.get("asset_browser"):
        hpath.append((host / ASSET_BROWSER_ADDON).as_posix())
    return hpath


def _install(
    archive_path,
    install_root,
    preference_root,
    data_root,
    allow_internal=False,
    replace_existing=False,
    expected_sha256=None,
    application_root=None,
    defer_commit=False,
    setup_token=None,
    isolated=False,
    blender_bridge=None,
    asset_browser=None,
):
    manifest = verify_archive(archive_path, expected_sha256)
    require(allow_internal or manifest["purpose"] == "public", "Internal candidate requires explicit --allow-internal")
    provider = archive_sdk_provider(archive_path)
    planned = package_enable(manifest["houdini"], provider.get("hdk_api"), provider=provider) if provider else None
    build = manifest.get("build_identity")
    identity = target_identity(manifest["houdini"], build)
    root = unlinked_path(install_root)
    preference_root, data_root = unlinked_path(preference_root), unlinked_path(data_root)
    require(
        preference_root != Path(preference_root.anchor) and data_root != Path(data_root.anchor),
        "Choose dedicated preference/data directories",
    )
    validate_install_paths(
        root, preference_root, data_root, manifest["houdini"], application_root, isolated, planned, build
    )
    root = _owned_root(root, create=True)
    marker = read_json(root / INSTALL_MARKER)
    require(
        marker.get("houdini", manifest["houdini"]) == manifest["houdini"],
        "Installation belongs to another Houdini target",
    )
    existing_state = read_json(root / "state.json") if (root / "state.json").exists() else {}
    require(
        not existing_state.get("active") or existing_state.get("houdini") == manifest["houdini"],
        "Installation belongs to a legacy or different target; choose a dedicated directory",
    )
    # A folder kept by an uninstalled build stays that build's: a community build never moves
    # into the official one's folder, nor the official build into a community one's.
    require(
        marker.get("package_filename", identity["package_filename"]) == identity["package_filename"],
        "This folder belongs to another Cycles for Houdini build; choose another folder",
    )
    if "houdini" not in marker:
        marker.update(identity)
        marker["application_root"] = str(unlinked_path(application_root) if application_root else root)
        marker["preference_root"] = str(preference_root)
        atomic_json(root / INSTALL_MARKER, marker)
    require(
        marker.get("preference_root") == str(preference_root), "Installation belongs to another Houdini preference root"
    )
    require(not _pending(root), "An interrupted transaction needs recover before installation")
    package_path = inside(preference_root, "packages/" + identity["package_filename"])
    state_path = root / "state.json"
    state = read_json(state_path) if state_path.exists() else {}
    components = install_components(state, blender_bridge, asset_browser)
    require(
        not state.get("active") or state.get("houdini") == manifest["houdini"],
        "Installation belongs to a legacy or different target; choose a dedicated directory",
    )
    legacy = _legacy_owner(preference_root)
    require(
        build is None or not legacy or legacy["manifest"]["houdini"] != manifest["houdini"],
        "An older official Cycles for Houdini is registered for this Houdini; uninstall it before installing a community build",
    )
    migrate = build is None and legacy and legacy["manifest"]["houdini"] == manifest["houdini"]
    if migrate:
        require(
            legacy["root"] != root and not overlaps(root, legacy["application_root"]),
            "Legacy migration requires a directory outside the old uninstaller tree",
        )
        require(not _pending(legacy["root"]), "Recover the legacy installation before migrating")
    old_value = _config_bytes(package_path)
    if state.get("active"):
        require(state["package_path"] == str(package_path), "Installation belongs to another Houdini preference root")
        require(
            old_value is not None and digest(old_value) == state["package_sha256"],
            "Active package configuration was changed externally",
        )
    else:
        require(
            old_value is None or replace_existing,
            "Existing Houdini package is unmanaged; --replace-existing preserves it for uninstall",
        )
    version = version_dir(root, manifest["package_id"])
    if not version.exists():
        staging = inside(root, ".staging/" + uuid.uuid4().hex[:VERSION_FOLDER_CHARS])
        staging.mkdir(parents=True)
        record_map = {entry["path"]: entry for entry in manifest["files"]}
        with Path(archive_path).open("rb") as archive_stream, zipfile.ZipFile(archive_stream) as archive:
            # Verify again on the same open handle used for extraction. A source
            # archive replaced after the first inspection cannot alter activation.
            extracted_manifest = _verify_archive(archive)
            require(extracted_manifest == manifest, "Archive changed while installing")
            for name in archive.namelist():
                target = inside(staging, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    value = archive.read(name)
                    if name != MANIFEST:
                        record = record_map[name]
                        require(digest(value) == record["sha256"], "Archive changed during extraction: " + name)
                    stream.write(value)
        verify_installed(staging, manifest["package_id"])
        version.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, version)
    else:
        verify_installed(version)
    host = version / "host"
    if components["asset_browser"] and not (host / ASSET_BROWSER_ADDON).is_dir():
        # Payloads built before the add-on existed cannot offer it. A kept
        # choice is dropped; an explicit request is refused.
        require(asset_browser is None, "This package does not contain the asset browser add-on")
        components["asset_browser"] = False
    provider_path = host / "resources/sdk-dependencies.json"
    provider = read_json(provider_path).get("openimagedenoise", {}) if provider_path.is_file() else {}
    previous_roots = state.get("registration_houdini_roots", [])
    require(
        isinstance(previous_roots, list) and all(isinstance(item, str) for item in previous_roots),
        "Invalid stored Houdini registration roots",
    )
    retained_roots = []
    for candidate in previous_roots:
        try:
            package_enable(
                manifest["houdini"], provider.get("hdk_api"), provider=provider, additional_roots=[candidate]
            )
        except PackageError:
            continue
        retained_roots.append(str(unlinked_path(candidate)))
    package = dict(
        enable=package_enable(
            manifest["houdini"], provider.get("hdk_api"), provider=provider, additional_roots=retained_roots
        ),
        load_package_once=True,
        hpath=package_hpath(host, components),
        env=[
            {"CYCLES_HOUDINI_HOST_ROOT": host.as_posix()},
            {"CYCLES_HOUDINI_RESOURCES": (host / "resources").as_posix()},
            {"CYCLES_HOUDINI_SOURCE_ROOT": data_root.as_posix()},
            {"CYCLES_HOUDINI_RELEASE_ID": manifest["package_id"]},
            {"CYCLES_HOUDINI_PACKAGE_FILE": identity["package_filename"]},
            {"CYCLES_HOUDINI_BLENDER_BRIDGE": "1" if components["blender_bridge"] else "0"},
            {"CYCLES_HOUDINI_ASSET_BROWSER": "1" if components["asset_browser"] else "0"},
            {"PATH": dict(method="prepend", value=["$HFS/bin", (version / "runtime/bin").as_posix()])},
        ],
    )
    value = canonical(package)
    validate_install_paths(
        root, preference_root, data_root, manifest["houdini"], application_root, isolated, package["enable"], build
    )
    state = dict(
        schema_version=SCHEMA,
        active=manifest["package_id"],
        package_path=str(package_path),
        preference_root=str(preference_root),
        houdini=manifest["houdini"],
        package_filename=identity["package_filename"],
        app_id=identity["app_id"],
        package_sha256=digest(value),
        original_package=state.get("original_package", _encoded(old_value)),
        archive_sha256=file_hash(archive_path),
        runtime_fingerprint=manifest["runtime_fingerprint"],
        registration_houdini_roots=sorted(set(retained_roots)),
        components=components,
    )
    if build is not None:
        state["build_identity"] = build_identity(build)
    if migrate:
        state["legacy_backup"] = dict(path=str(legacy["path"]), original_package=legacy["state"]["original_package"])
        retired = dict(schema_version=SCHEMA, active=None, migrated_to=str(root), houdini=manifest["houdini"])
        with installation_lock(legacy["root"]):
            # Recheck ownership under the old root's lock before journaling handover.
            require(_legacy_owner(preference_root)["state"] == legacy["state"], "Legacy state changed during migration")
            _activate(
                root,
                package_path,
                value,
                state,
                "migrate-legacy",
                [(legacy["path"], None), (legacy["root"] / "state.json", canonical(retired))],
                old_value,
                defer_commit,
                setup_token,
            )
    else:
        previous_state = read_json(state_path) if state_path.exists() else {}
        if previous_state.get("legacy_backup"):
            state["legacy_backup"] = previous_state["legacy_backup"]
        _activate(
            root,
            package_path,
            value,
            state,
            "install",
            expected_previous=old_value,
            defer_commit=defer_commit,
            setup_token=setup_token,
        )
    return dict(
        result="passed",
        package_id=manifest["package_id"],
        host=str(host),
        configuration=str(package_path),
        archive_sha256=state["archive_sha256"],
        purpose=manifest["purpose"],
        components=components,
    )


def install(
    archive_path,
    install_root,
    preference_root,
    data_root,
    allow_internal=False,
    replace_existing=False,
    expected_sha256=None,
    application_root=None,
    defer_commit=False,
    setup_token=None,
    isolated=False,
    blender_bridge=None,
    asset_browser=None,
):
    manifest = verify_archive(archive_path, expected_sha256)
    houdini = manifest["houdini"]
    build = manifest.get("build_identity")
    identity = target_identity(houdini, build)
    require(allow_internal or manifest["purpose"] == "public", "Internal candidate requires explicit --allow-internal")
    root, prefs, data = map(unlinked_path, (install_root, preference_root, data_root))
    problem = install_path_problem(
        root,
        [entry["path"] for entry in manifest["files"]] + [MANIFEST],
        application_root,
        preference_root=prefs,
        package_filename=identity["package_filename"],
        package_id=manifest["package_id"],
        houdini=houdini,
    )
    require(problem is None, problem)
    provider = archive_sdk_provider(archive_path)
    planned = package_enable(houdini, provider.get("hdk_api"), provider=provider) if provider else None
    validate_install_paths(root, prefs, data, houdini, application_root, isolated, planned, build)
    root = _owned_root(root, create=True)
    prefs.mkdir(parents=True, exist_ok=True)
    with installation_lock(prefs), installation_lock(root):
        return _install(
            archive_path,
            root,
            prefs,
            data_root,
            allow_internal,
            replace_existing,
            expected_sha256,
            application_root,
            defer_commit,
            setup_token,
            isolated,
            blender_bridge,
            asset_browser,
        )


def refresh_registration(install_root, houdini_roots=()):
    """Recheck installed SDKs and update only this installation's owned gate."""
    root = _owned_root(install_root)
    state = read_json(root / "state.json")
    require(state.get("active"), "No active Cycles installation to refresh")
    prefs = unlinked_path(state["preference_root"])
    with installation_lock(prefs), installation_lock(root):
        require(not _pending(root), "Recover the interrupted transaction before refreshing registration")
        state = read_json(root / "state.json")
        require(state.get("active"), "No active Cycles installation to refresh")
        manifest = verify_installed(version_dir(root, state["active"]))
        require(manifest["houdini"] == state["houdini"], "Installed Houdini target differs from its receipt")
        path = _package_path(state)
        current = _config_bytes(path)
        require(
            current is not None and digest(current) == state["package_sha256"],
            "Active package configuration was changed externally",
        )
        package = json.loads(current)
        require(isinstance(package, dict), "Active package configuration is not an object")
        package_eligible_versions(package.get("enable"))
        host = version_dir(root, state["active"]) / "host"
        require(
            package.get("hpath") == package_hpath(host, install_components(state))
            and any(item.get("CYCLES_HOUDINI_HOST_ROOT") == host.as_posix() for item in package.get("env", [])),
            "Active package host differs from its installed payload",
        )
        provider = read_json(host / "resources/sdk-dependencies.json")["openimagedenoise"]
        hdk_api = provider.get("hdk_api")
        require(hdk_api is not None, "Installed package lacks a frozen HDK API; reinstall to refresh")
        saved_roots = state.get("registration_houdini_roots", [])
        require(
            isinstance(saved_roots, list) and all(isinstance(item, str) for item in saved_roots),
            "Invalid stored Houdini registration roots",
        )
        saved = {str(unlinked_path(item)) for item in saved_roots}
        requested = {str(unlinked_path(item)) for item in houdini_roots}
        retained, removed = set(), []
        for candidate in sorted(saved | requested):
            try:
                package_enable(manifest["houdini"], hdk_api, provider=provider, additional_roots=[candidate])
            except PackageError as error:
                if candidate in requested and candidate not in saved:
                    raise
                removed.append(dict(path=candidate, reason=str(error)))
            else:
                retained.add(candidate)
        enable = package_enable(manifest["houdini"], hdk_api, provider=provider, additional_roots=sorted(retained))
        proposed = package_eligible_versions(enable)
        for other in (prefs / "packages").glob("cycles_houdini*.json"):
            if unlinked_path(other) == path:
                continue
            try:
                existing = package_eligible_versions(read_json(other).get("enable"))
            except (OSError, ValueError, KeyError) as error:
                raise PackageError(f"Unparseable Cycles package registration: {other}") from error
            require(
                not eligible_sets_overlap(proposed, existing),
                f"Another Cycles package is eligible for the same Houdini build: {other.name}",
            )
        package["enable"] = enable
        value = canonical(package)
        changed = value != current or sorted(retained) != sorted(saved)
        if changed:
            updated = dict(state, package_sha256=digest(value), registration_houdini_roots=sorted(retained))
            _activate(root, path, value, updated, "refresh-registration", expected_previous=current)
        return dict(
            result="passed",
            changed=changed,
            package_id=state["active"],
            configuration=str(path),
            eligible_versions=sorted(proposed),
            registered_houdini_roots=sorted(retained),
            removed_houdini_roots=removed,
        )


def verify_installed(version, expected_id=None):
    version = unlinked_path(version)
    manifest = read_json(version / MANIFEST)
    identity = dict(manifest)
    package_id = identity.pop("package_id")
    require(
        digest(canonical(identity)) == package_id
        and (
            expected_id == package_id
            if expected_id
            else version.name in (package_id, package_id[:VERSION_FOLDER_CHARS])
        ),
        "Installed release identity mismatch",
    )
    expected = {entry["path"] for entry in manifest["files"]} | {MANIFEST}
    actual = {p.relative_to(version).as_posix() for p in version.rglob("*") if p.is_file()}
    # Python bytecode created by Houdini is not part of an immutable payload.
    require(
        {name for name in actual if _runtime_file(name)} == expected, "Installed payload files differ from manifest"
    )
    for entry in manifest["files"]:
        path = inside(version, entry["path"])
        require(
            path.is_file() and path.stat().st_size == entry["bytes"] and file_hash(path) == entry["sha256"],
            "Installed payload hash mismatch: " + entry["path"],
        )
    verify_public_binding(manifest, lambda path: inside(version, path).read_bytes())
    return manifest


def _package_path(state):
    filename = state.get("package_filename", PACKAGE_FILE)
    require(
        filename == PACKAGE_FILE
        or filename == target_identity(state["houdini"], state.get("build_identity"))["package_filename"],
        "Invalid stored package filename",
    )
    path = inside(state["preference_root"], "packages/" + filename)
    require(str(path) == state["package_path"], "Stored package path differs from its preference root")
    return path


def _uninstall(install_root):
    root = _owned_root(install_root)
    require(not _pending(root), "Recover the interrupted transaction before uninstalling")
    state = read_json(root / "state.json")
    require(state.get("active"), "No active installation")
    path = _package_path(state)
    current = _config_bytes(path)
    require(
        current is not None and digest(current) == state["package_sha256"],
        "Configuration was changed externally; preserving it",
    )
    restored = _decoded(state["original_package"])
    extra = []
    if state.get("legacy_backup"):
        backup = state["legacy_backup"]
        legacy_path = inside(state["preference_root"], "packages/" + PACKAGE_FILE)
        require(
            str(legacy_path) == backup["path"] and not legacy_path.exists(),
            "Legacy package changed after migration; preserving it",
        )
        extra.append((legacy_path, _decoded(backup["original_package"])))
    _activate(
        root,
        path,
        restored,
        dict(schema_version=SCHEMA, active=None, houdini=state.get("houdini")),
        "uninstall",
        extra,
        current,
    )
    return dict(
        result="passed",
        configuration=str(path),
        restored_previous=restored is not None,
        retained_versions=str(root / "versions"),
        reason="Immutable payloads retained for rollback; no running DLLs are deleted.",
    )


def uninstall(install_root):
    root = _owned_root(install_root)
    state = read_json(root / "state.json")
    prefs = unlinked_path(state["preference_root"]) if state.get("active") else root
    with installation_lock(prefs), installation_lock(root) if prefs != root else nullcontext():
        return _uninstall(root)


def _transaction_build(transaction):
    """The build identity a journal belongs to (None for the official build).

    Journals written before they carried the identity take it from the
    installation state they record."""
    if "build_identity" in transaction:
        return build_identity(transaction["build_identity"])
    for state in (transaction.get("intended_state"), transaction.get("previous_state")):
        if isinstance(state, dict) and state.get("build_identity") is not None:
            return build_identity(state["build_identity"])
    return None


def _validate_transaction(root, transaction):
    """A journal cannot grant ownership over arbitrary filesystem paths."""
    build = _transaction_build(transaction)
    package_path = _package_path(dict(transaction, build_identity=build))
    build_id = (build or {}).get("build_id")
    for state in (transaction.get("intended_state"), transaction.get("previous_state")):
        if isinstance(state, dict) and state.get("active"):
            require(
                (state.get("build_identity") or {}).get("build_id") == build_id,
                "Transaction build identity differs from its installation state",
            )
    marker = read_json(root / INSTALL_MARKER)
    if marker.get("houdini"):
        require(
            transaction.get("installation_id") == marker["installation_id"]
            and transaction.get("houdini") == marker["houdini"]
            and transaction.get("preference_root") == marker["preference_root"]
            and transaction.get("package_filename") == marker["package_filename"]
            and marker.get("build_id") == build_id,
            "Transaction belongs to another installation or Houdini target",
        )
    for key in ("previous_state", "intended_state"):
        if key + "_bytes" in transaction:
            value = _decoded(transaction[key + "_bytes"])
            require(
                (json.loads(value) if value is not None else None) == transaction.get(key),
                "Transaction state bytes differ from its receipt",
            )
    current_state = read_json(root / "state.json") if (root / "state.json").exists() else None
    require(
        current_state in (transaction.get("previous_state"), transaction.get("intended_state")),
        "Installation state changed after interruption; preserving it",
    )
    extras = transaction.get("extra_changes", [])
    generic = inside(transaction["preference_root"], "packages/" + PACKAGE_FILE)
    if not extras:
        return package_path, None
    operation = transaction["operation"]
    require(operation in ("migrate-legacy", "uninstall"), "Unexpected transaction side effects")
    require(Path(extras[0]["path"]) == generic, "Transaction side effect escapes its owned registration")
    if operation == "uninstall":
        require(len(extras) == 1 and extras[0]["previous"] is None, "Unexpected uninstall side effects")
        state = transaction["previous_state"]
        require(
            state.get("legacy_backup", {}).get("original_package") == extras[0]["intended"],
            "Legacy backup differs from receipt",
        )
        return package_path, None
    require(len(extras) == 2 and extras[0]["intended"] is None, "Unexpected migration side effects")
    owner = transaction.get("legacy_owner", {})
    legacy = _owned_root(owner["root"])
    require(
        read_json(legacy / INSTALL_MARKER)["installation_id"] == owner["installation_id"],
        "Legacy installation ownership changed",
    )
    require(
        Path(extras[1]["path"]) == legacy / "state.json" and not overlaps(root, legacy),
        "Transaction side effect escapes its legacy owner",
    )
    state = json.loads(_decoded(extras[1]["previous"]))
    require(
        state.get("package_path") == str(generic)
        and state["package_sha256"] == digest(_decoded(extras[0]["previous"])),
        "Legacy transaction receipt does not own its registration",
    )
    manifest = verify_installed(version_dir(legacy, state["active"]))
    require(manifest["houdini"] == transaction["houdini"], "Legacy transaction targets another Houdini build")
    retired = json.loads(_decoded(extras[1]["intended"]))
    require(
        retired.get("active") is None and retired.get("migrated_to") == str(root), "Invalid legacy retirement receipt"
    )
    return package_path, legacy


def _recover(install_root):
    root = _owned_root(install_root)
    recovered = []
    for path in _pending(root):
        transaction = read_json(path)
        package_path, legacy = _validate_transaction(root, transaction)
        with installation_lock(legacy) if legacy else nullcontext():
            _recover_transaction(root, path, transaction, package_path)
        recovered.append(path.name)
    return dict(result="passed", recovered=recovered)


def _recover_transaction(root, path, transaction, package_path):
    current = _config_bytes(package_path)
    old, intended = _decoded(transaction["previous"]), _decoded(transaction["intended"])
    require(current in (old, intended), "Configuration changed after interruption; refusing to overwrite it")
    extras = transaction.get("extra_changes", [])
    # Validate every affected file first, so a conflict cannot cause a partial recovery.
    for change in extras:
        destination = unlinked_path(change["path"])
        require(
            _config_bytes(destination) in (_decoded(change["previous"]), _decoded(change["intended"])),
            "Configuration changed after interruption; refusing to overwrite it",
        )
    for change in reversed(extras):
        destination, content = unlinked_path(change["path"]), _decoded(change["previous"])
        if content is None:
            if destination.exists():
                destination.unlink()
        else:
            atomic_bytes(destination, content)
    if current != old:
        if old is None:
            package_path.unlink()
        else:
            atomic_bytes(package_path, old)
    previous_state = transaction["previous_state"]
    if previous_state is None:
        (root / "state.json").unlink(missing_ok=True)
    elif "previous_state_bytes" in transaction:
        atomic_bytes(root / "state.json", _decoded(transaction["previous_state_bytes"]))
    else:
        atomic_json(root / "state.json", previous_state)
    transaction["state"] = "rolled_back"
    atomic_json(path, transaction)


def recover(install_root):
    root = _owned_root(install_root)
    pending = _pending(root)
    prefs = unlinked_path(read_json(pending[0])["preference_root"]) if pending else root
    with installation_lock(prefs), installation_lock(root) if prefs != root else nullcontext():
        return _recover(root)


def commit_setup(install_root, setup_token=None):
    root = _owned_root(install_root)
    state = read_json(root / "state.json")
    with installation_lock(unlinked_path(state["preference_root"])), installation_lock(root):
        pending = _pending(root)
        require(len(pending) == 1, "Expected one pending Setup activation")
        path = pending[0]
        transaction = read_json(path)
        require(
            transaction.get("setup_owned") and transaction["state"] == "setup_pending",
            "Transaction is not owned by Setup",
        )
        require(transaction.get("setup_token") == setup_token, "Setup transaction token differs")
        package, legacy = _validate_transaction(root, transaction)
        with installation_lock(legacy) if legacy else nullcontext():
            require(
                state == transaction["intended_state"] and _config_bytes(package) == _decoded(transaction["intended"]),
                "Setup activation changed before commit",
            )
            verify_installed(version_dir(root, state["active"]))
            for change in transaction.get("extra_changes", []):
                require(
                    _config_bytes(change["path"]) == _decoded(change["intended"]),
                    "Setup migration changed before commit",
                )
            transaction["state"] = "committed"
            atomic_json(path, transaction)
    return dict(result="passed", package_id=state["active"])


def abort_setup(install_root, setup_token):
    root = unlinked_path(install_root)
    if not (root / INSTALL_MARKER).exists():
        return dict(result="passed", recovered=[])
    matching = [
        path
        for path in _pending(root)
        if read_json(path).get("setup_owned") and read_json(path).get("setup_token") == setup_token
    ]
    if not matching:
        return dict(result="passed", recovered=[])
    transaction = read_json(matching[0])
    with installation_lock(unlinked_path(transaction["preference_root"])), installation_lock(root):
        require(_pending(root) == matching, "Another operation owns an interrupted transaction")
        return _recover(root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", help="Write tool output directly to this file (no command shell)")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("audit", "build"):
        item = sub.add_parser(command)
        item.add_argument("--build-root", required=True)
        item.add_argument("--source-root", required=True)
        item.add_argument("--report", required=True)
        item.add_argument("--purpose", choices=("internal", "public"), default="internal")
        item.add_argument("--public-sdk-root", action="append", default=[])
        if command == "build":
            item.add_argument("--output", required=True)
            item.add_argument("--certification")
            item.add_argument("--build-identity", type=Path, help="Community build identity (build-identity.json)")
            item.add_argument("--build-record", type=Path, help="How the community build was made (JSON)")
    item = sub.add_parser("verify")
    item.add_argument("archive")
    item.add_argument("--expected-sha256")
    item = sub.add_parser("verify-build-provenance")
    item.add_argument("--version-root", type=Path, required=True)
    item.add_argument("--build-manifest", type=Path, required=True)
    item = sub.add_parser("prepare-identity", help="Fill in a community build identity on first use")
    item.add_argument("--build-identity", type=Path, required=True)
    item = sub.add_parser("identity")
    item.add_argument("--houdini", required=True)
    item.add_argument("--build-identity", type=Path, help="Community build identity (build-identity.json)")
    item.add_argument("--archive", help="Take the build identity from this verified package")
    item = sub.add_parser("package-enable")
    item.add_argument("--houdini", required=True)
    item.add_argument("--hdk-api", type=int, required=True)
    item.add_argument("--provider-json", type=Path, required=True)
    item = sub.add_parser(
        "registration-status", help="Read-only Setup consent preflight; exit 2 means consent is required"
    )
    item.add_argument("--houdini", required=True)
    item.add_argument("--install-root", required=True)
    item.add_argument("--preference-root", required=True)
    item.add_argument("--data-root", required=True)
    item.add_argument("--application-root")
    item.add_argument("--archive", help="Verified package archive for precise eligibility preflight")
    item.add_argument("--build-id", help="Community build id the Setup was made for (requires --archive)")
    item = sub.add_parser("install")
    item.add_argument("archive")
    item.add_argument("--install-root", required=True)
    item.add_argument("--preference-root", required=True)
    item.add_argument("--data-root", required=True)
    item.add_argument("--application-root")
    item.add_argument("--expected-sha256")
    item.add_argument("--allow-internal", action="store_true")
    item.add_argument(
        "--isolated",
        action="store_true",
        help="Verify in private directories alongside an existing Windows installation",
    )
    item.add_argument("--replace-existing", action="store_true")
    item.add_argument(
        "--defer-commit",
        action="store_true",
        help="Setup owns this activation until commit-setup; recover rolls it back",
    )
    item.add_argument("--setup-token")
    item.add_argument(
        "--blender-bridge",
        choices=("enabled", "disabled"),
        help="Select the optional .blend asset bridge; omission preserves this installation's selection",
    )
    item.add_argument(
        "--asset-browser",
        choices=("enabled", "disabled"),
        help="Select the optional asset browser (requires the bridge); omission preserves this installation's"
        " selection, and a disabled bridge disables it",
    )
    item = sub.add_parser("refresh-registration", help="Recheck installed Houdini builds for the active package")
    item.add_argument("--install-root", required=True)
    item.add_argument("--houdini-root", action="append", default=[], help="Additional nondefault Houdini installation")
    for command in ("uninstall", "recover", "status", "commit-setup", "abort-setup"):
        item = sub.add_parser(command)
        item.add_argument("--install-root", required=True)
        if command in ("commit-setup", "abort-setup"):
            item.add_argument("--setup-token", required=True)
    args = parser.parse_args(argv)
    if args.log:
        forwarded = list(sys.argv[1:] if argv is None else argv)
        at = forwarded.index("--log")
        del forwarded[at : at + 2]
        with Path(args.log).open("w", encoding="utf-8") as log, redirect_stdout(log), redirect_stderr(log):
            return main(forwarded)
    try:
        if args.command in ("audit", "build"):
            community = args.command == "build" and args.build_identity is not None
            plan = payload_plan(args.build_root, args.source_root, community=community)
            report, notices = audit(plan)
            if args.command == "audit" and args.purpose == "public" and report["result"] == "passed":
                try:
                    files, manifest = archive_contents(plan, report, notices, "public", args.public_sdk_root)
                    fields = public_certificate_fields(manifest, files[BUILD_PROVENANCE])
                    fields.pop("result")
                    report["public_certificate_bindings"] = fields
                except (KeyError, ValueError, TypeError, PackageError) as error:
                    report.update(result="blocked", public_provenance_error=str(error))
            if args.command == "build":
                try:
                    report["package"] = make_archive(
                        args.build_root,
                        args.source_root,
                        args.output,
                        args.purpose,
                        args.certification,
                        args.public_sdk_root,
                        read_json(args.build_identity) if community else None,
                        read_json(args.build_record) if community and args.build_record else None,
                    )
                except PackageError as error:
                    report.update(result="blocked", build_error=str(error))
            atomic_json(args.report, report)
            result = dict(result=report["result"], report=str(Path(args.report).resolve()), gates=report["gates"])
            if "package" in report:
                result["package"] = report["package"]
            print(json.dumps(result))
            return 0 if args.command == "build" and "package" in report or report["result"] == "passed" else 2
        if args.command == "verify-build-provenance":
            manifest = verify_installed(args.version_root)
            package_bytes = inside(args.version_root, BUILD_PROVENANCE).read_bytes()
            require(
                matches_build_provenance(manifest, package_bytes, args.build_manifest.read_bytes()),
                "Installed payload belongs to a different build manifest",
            )
            print(json.dumps(dict(result="passed", package_id=manifest["package_id"])))
            return 0
        if args.command == "package-enable":
            result = dict(
                enable=package_enable(
                    args.houdini, args.hdk_api, provider=read_json(args.provider_json)["openimagedenoise"]
                )
            )
        elif args.command == "prepare-identity":
            result = prepare_build_identity(args.build_identity)
        elif args.command == "identity":
            build = read_json(args.build_identity) if args.build_identity else None
            if args.archive:
                require(build is None, "Pass --build-identity or --archive, not both")
                build = verify_archive(args.archive).get("build_identity")
            result = target_identity(args.houdini, build)
        elif args.command == "registration-status":
            result = registration_status(
                args.install_root,
                args.preference_root,
                args.data_root,
                args.houdini,
                args.application_root,
                args.archive,
                args.build_id,
            )
            print(json.dumps(result))
            return 2 if result["replace_existing_required"] else 0
        elif args.command == "verify":
            manifest = verify_archive(args.archive, args.expected_sha256)
            result = dict(
                result="passed",
                package_id=manifest["package_id"],
                runtime_fingerprint=manifest["runtime_fingerprint"],
                purpose=manifest["purpose"],
            )
        elif args.command == "install":
            result = install(
                args.archive,
                args.install_root,
                args.preference_root,
                args.data_root,
                args.allow_internal,
                args.replace_existing,
                args.expected_sha256,
                args.application_root,
                args.defer_commit,
                args.setup_token,
                args.isolated,
                None if args.blender_bridge is None else args.blender_bridge == "enabled",
                None if args.asset_browser is None else args.asset_browser == "enabled",
            )
        elif args.command == "refresh-registration":
            result = refresh_registration(args.install_root, args.houdini_root)
        elif args.command == "uninstall":
            result = uninstall(args.install_root)
        elif args.command == "recover":
            result = recover(args.install_root)
        elif args.command == "commit-setup":
            result = commit_setup(args.install_root, args.setup_token)
        elif args.command == "abort-setup":
            result = abort_setup(args.install_root, args.setup_token)
        else:
            root = _owned_root(args.install_root)
            state = read_json(root / "state.json") if (root / "state.json").exists() else {}
            if state.get("active"):
                verify_installed(version_dir(root, state["active"]))
                current = _config_bytes(_package_path(state))
                require(
                    current is not None and digest(current) == state["package_sha256"],
                    "Active configuration differs from receipt",
                )
            result = dict(result="passed", state=state, pending_transactions=[p.name for p in _pending(root)])
            if state.get("active"):
                # Effective optional components, including defaults for legacy receipts.
                result["components"] = install_components(state)
        print(json.dumps(result))
        return 0
    except (PackageError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(json.dumps(dict(result="failed", error=str(error))), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
