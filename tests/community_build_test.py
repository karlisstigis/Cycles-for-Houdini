# SPDX-License-Identifier: Apache-2.0
"""Community builds: their own installer identity, and the public engine edit loop.

Offline: a local repository stands in for upstream Cycles.
"""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = runpy.run_path(str(ROOT / "scripts/release-package.py"))
FORK = runpy.run_path(str(ROOT / "scripts/engine-fork.py"))
UPDATE = runpy.run_path(str(ROOT / "scripts/update-engine-patches.py"))
IDENTITY = runpy.run_path(str(ROOT / "scripts/engine_source.py"))


def git(repo, *args):
    return subprocess.check_output(["git", "-c", "core.autocrlf=false", "-C", str(repo), *args]).decode().strip()


def community(**changes):
    value = dict(
        schema_version=1,
        build_id="community",
        build_name="Community build",
        publisher="Unnamed community builder",
        publisher_url="",
        dlss="off",
        dlss_project_id="",
    )
    value.update(changes)
    return value


class BuildIdentityTest(unittest.TestCase):
    def test_official_identity_is_unchanged(self):
        self.assertEqual(
            PACKAGE["target_identity"]("22.0.466"),
            dict(
                houdini="22.0.466",
                platform="windows-x64",
                app_id="{B451136F-8173-5CC3-9C60-2A37259A915F}",
                package_filename="cycles_houdini_22_0_466.json",
                default_directory="Cycles for Houdini 22.0.466",
            ),
        )

    def test_community_identity_never_shares_the_official_one(self):
        official = PACKAGE["target_identity"]("22.0.466")
        mine = PACKAGE["target_identity"]("22.0.466", community())
        for key in ("app_id", "package_filename", "default_directory"):
            self.assertNotEqual(mine[key], official[key])
        self.assertEqual(mine["package_filename"], "cycles_houdini_community_community_22_0_466.json")
        self.assertEqual(mine["display_name"], "Cycles for Houdini - community build")
        # The Apps entry and Start-menu name always say "community build", whatever the name.
        for name in ("Houdini 22.0.466", "Anna hair fix"):
            label = PACKAGE["target_identity"]("22.0.466", community(build_name=name))["display_name"]
            self.assertEqual(label, "Cycles for Houdini - community build (" + name + ")")
        other = PACKAGE["target_identity"]("22.0.466", community(build_id="anna-hair-fix"))
        self.assertNotEqual(other["app_id"], mine["app_id"])
        # The shared-preferences check still sees every Cycles package file.
        self.assertTrue(mine["package_filename"].startswith("cycles_houdini"))
        # Deterministic: the same identity always gives the same Windows application id.
        self.assertEqual(PACKAGE["target_identity"]("22.0.466", community())["app_id"], mine["app_id"])

    def test_invalid_identities_are_refused(self):
        for bad in (
            community(build_id="official"),
            community(build_id="my-official-build"),
            community(build_name="Official Cycles"),
            community(build_id="A"),
            community(build_id="9lives"),
            community(publisher=""),
            community(publisher='say "hi"'),
            # Characters that break the Start-menu folder (":" is refused by Windows, "/" makes a subfolder).
            community(build_name="Hair: fix"),
            community(build_name="Hair/fix"),
            community(build_name="Hair\\fix"),
            community(build_name="Hair*?"),
            community(publisher="Anna: hair"),
            community(publisher_url="ftp://example.invalid"),
            community(publisher_url="https:///github.com/community-builder"),
            community(publisher_url="https://example.invalid/\N{LATIN SMALL LETTER LONG S}"),
            community(dlss="on"),
            # The official publisher in any spelling (case, spaces, punctuation), also inside a web address.
            community(publisher="The Cycles for Houdini Authors"),
            community(publisher="the cycles for houdini authors"),
            community(publisher="The Cycles for Houdini Authors."),
            community(publisher="T.H.E. C-Y-C-L-E-S for Houdini Authors"),
            community(build_name="The Cycles for Houdini Authors edition"),
            community(publisher_url="https://example.invalid/thecyclesforhoudiniauthors"),
            community(schema_version=2),
        ):
            with self.subTest(bad=bad), self.assertRaises(PACKAGE["PackageError"]):
                PACKAGE["build_identity"](bad)

    def test_official_publisher_repo_and_its_pages_are_reserved(self):
        official = PACKAGE["OFFICIAL_PUBLISHER_URL"]
        host, owner, repo = official.removeprefix("https://").split("/")
        for url in (
            official,
            official + "/",
            official + "?tab=readme#readme",
            official + "/issues/5",
            official + ".git",
            f"HTTP://WWW.{host.upper()}:80/{owner.upper()}/{repo.lower()}/",
            f"https://{host}.:443/{owner}/{repo}",
            f"https://builder@{host}:8443/{owner}/{repo}/releases",
            f"https://{host}/{owner}/./{repo.replace('-', '%2D')}",
            f"https://%67{host[1:]}/{owner}/{repo}",
        ):
            with self.subTest(url=url), self.assertRaisesRegex(PACKAGE["PackageError"], "own web address"):
                PACKAGE["build_identity"](community(publisher_url=url))

    def test_a_fork_or_other_address_can_be_its_own_publisher_url(self):
        official = PACKAGE["OFFICIAL_PUBLISHER_URL"]
        host, owner, repo = official.removeprefix("https://").split("/")
        for url in (
            "",
            f"https://{host}/community-builder/{repo}",
            f"https://{host}/community-builder/{repo}/releases",
            f"https://{host}/{owner}/{repo}-fork",
            f"https://{host}/{owner}/{repo}.git-tools",
            f"https://{host}/{owner}-mirror/{repo}",
            f"https://{host}/{owner}",
            f"https://{host}.example.invalid/{owner}/{repo}",
            f"https://{host}@example.invalid/{owner}/{repo}",
            f"https://example.invalid/?upstream={official}",
        ):
            with self.subTest(url=url):
                target = PACKAGE["target_identity"]("22.0.466", community(publisher_url=url))
                self.assertEqual(target["publisher_url"], url)

    def test_each_builder_gets_an_id_of_their_own_on_first_use(self):
        template = json.loads((ROOT / "build-identity.json").read_text(encoding="utf-8"))
        self.assertEqual(template["build_id"], "")  # filled in on first use
        with tempfile.TemporaryDirectory() as temporary:
            ids = []
            for index, (publisher, pattern) in enumerate(
                (
                    ("Unnamed community builder", r"builder-[0-9a-f]{6}"),
                    ("Unnamed community builder", r"builder-[0-9a-f]{6}"),
                    ("Anna Berg", r"anna-berg-[0-9a-f]{6}"),
                    ("3D Studio Nord", r"builder-3d-studio-nord-[0-9a-f]{6}"),
                    ("A. B.", r"a-b-[0-9a-f]{6}"),
                )
            ):
                path = Path(temporary) / f"identity-{index}.json"
                raw = json.dumps(dict(template, publisher=publisher), indent=2) + "\n"
                path.write_bytes(raw.replace("\n", "\r\n").encode() if index == 1 else raw.encode())
                self.assertEqual(PACKAGE["prepare_build_identity"](path)["filled"], ["build_id", "dlss_project_id"])
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertRegex(value["build_id"], "^" + pattern + "$")
                self.assertEqual(b"\r\n" in path.read_bytes(), index == 1)  # line endings kept
                self.assertEqual(PACKAGE["build_identity"](value)["build_id"], value["build_id"])
                before = path.read_bytes()
                self.assertEqual(PACKAGE["prepare_build_identity"](path)["filled"], [])  # never changed again
                self.assertEqual(path.read_bytes(), before)
                ids.append(value["build_id"])
            self.assertNotEqual(ids[0], ids[1])  # two unnamed builders, two ids
            mine = Path(temporary) / "mine.json"
            mine.write_text(json.dumps(community(build_id="anna-hair-fix")))
            self.assertEqual(PACKAGE["prepare_build_identity"](mine)["build_id"], "anna-hair-fix")

    def test_dlss_is_on_with_an_id_of_your_own(self):
        official = re.search(
            rb'ngx_project_id\[\] = "([0-9a-f-]+)"', (ROOT / "patches/0077-houdini-dlss-project.patch").read_bytes()
        )[1].decode()
        self.assertEqual(PACKAGE["OFFICIAL_DLSS_PROJECT_ID"], official)
        template = json.loads((ROOT / "build-identity.json").read_text(encoding="utf-8"))
        self.assertEqual((template["dlss"], template["dlss_project_id"]), ("on", ""))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "identity.json"
            path.write_text(json.dumps(template, indent=2))
            self.assertEqual(PACKAGE["prepare_build_identity"](path)["filled"], ["build_id", "dlss_project_id"])
            value = json.loads(path.read_text())
            self.assertRegex(
                value["dlss_project_id"], "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
            )
            self.assertNotEqual(value["dlss_project_id"], official)
            self.assertEqual(PACKAGE["build_identity"](value)["dlss_project_id"], value["dlss_project_id"])
            other = Path(temporary) / "other.json"
            other.write_text(json.dumps(template, indent=2))
            PACKAGE["prepare_build_identity"](other)
            self.assertNotEqual(json.loads(other.read_text())["dlss_project_id"], value["dlss_project_id"])
            # "off" gets no id.
            off = Path(temporary) / "off.json"
            off.write_text(json.dumps(dict(template, dlss="off"), indent=2))
            self.assertEqual(PACKAGE["prepare_build_identity"](off)["filled"], ["build_id"])
        good = "a0f57b54-1daf-4934-90ae-c4035c19df04"
        self.assertEqual(PACKAGE["build_identity"](community(dlss="on", dlss_project_id=good))["dlss"], "on")
        self.assertEqual(
            PACKAGE["build_identity"](community(dlss="official-project-id-with-permission"))["dlss_project_id"], ""
        )
        for bad in (
            community(dlss="on"),  # no id
            community(dlss="on", dlss_project_id=official),  # the official id needs the explicit setting
            community(dlss="on", dlss_project_id=good.upper()),
            community(dlss="on", dlss_project_id="{" + good + "}"),
            community(dlss="off", dlss_project_id=good),
            community(dlss="own-project-id"),  # a value used during early development
        ):
            with self.subTest(bad=bad), self.assertRaises(PACKAGE["PackageError"]):
                PACKAGE["build_identity"](bad)

    @unittest.skipUnless(
        shutil.which("cmake") and (shutil.which("ninja") or (os.name != "nt" and shutil.which("make"))),
        "cmake with Ninja (or make) is not on PATH",
    )
    def test_cmake_compiles_a_community_dlss_id_only_when_asked(self):
        """The CMakeLists.txt block on a stand-in engine: configure only, no compiler needed."""
        text = (ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
        block = text[text.index("# Community builds (docs/dev/community-builds.md)") : text.index("if(WIN32)")]
        official = PACKAGE["OFFICIAL_DLSS_PROJECT_ID"]
        mine = "a0f57b54-1daf-4934-90ae-c4035c19df04"
        source = '/* @param kept */\nstatic constexpr char ngx_project_id[] = "' + official + '";\nint x;\n'
        with tempfile.TemporaryDirectory() as temporary:
            top = Path(temporary)
            (top / "engine/src/integrator").mkdir(parents=True)
            (top / "engine/src/integrator/denoiser_dlss.cpp").write_text(source)
            (top / "engine/src/integrator/other.cpp").write_text("int y;\n")
            (top / "engine/CMakeLists.txt").write_text(
                "add_library(cycles_integrator INTERFACE src/integrator/other.cpp src/integrator/denoiser_dlss.cpp)\n"
            )
            (top / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.24)\nproject(t NONE)\nset(CH_DLSS ON)\n"
                'set(CH_ENGINE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/engine")\nadd_subdirectory("${CH_ENGINE_ROOT}" cycles)\n'
                + block.replace(
                    'list(FIND ch_dlss_sources "denoiser_dlss.cpp"',
                    'list(FIND ch_dlss_sources "src/integrator/denoiser_dlss.cpp"',
                )
                + 'get_target_property(s cycles_integrator SOURCES)\nmessage(STATUS "SOURCES=${s}")\n'
            )

            def configure(*definitions):
                generator = ["-G", "Ninja"] if shutil.which("ninja") else ["-G", "Unix Makefiles"]
                done = subprocess.run(
                    ["cmake", *generator, "-S", str(top), "-B", str(top / "b"), *definitions],
                    capture_output=True,
                    text=True,
                )
                return done.returncode, done.stdout + done.stderr

            copy = top / "b/community-dlss/denoiser_dlss.cpp"
            code, log = configure()  # the official route passes nothing
            self.assertEqual(code, 0, log)
            self.assertIn("SOURCES=src/integrator/other.cpp;src/integrator/denoiser_dlss.cpp", log)
            self.assertFalse(copy.exists())
            code, log = configure("-DCH_DLSS_PROJECT_ID=" + mine)
            self.assertEqual(code, 0, log)
            self.assertIn("SOURCES=src/integrator/other.cpp;" + str(copy).replace("\\", "/"), log)
            self.assertEqual(copy.read_text(), source.replace(official, mine))  # only the id differs
            self.assertEqual(configure("-DCH_DLSS_PROJECT_ID=")[0], 0)
            self.assertFalse(copy.exists())
            self.assertNotEqual(configure("-DCH_DLSS_PROJECT_ID=" + mine.upper())[0], 0)
            # The engine source itself is never changed.
            self.assertEqual((top / "engine/src/integrator/denoiser_dlss.cpp").read_text(), source)

    def test_community_package_cannot_be_a_certified_public_release(self):
        plan = dict(
            runtime_overrides={}, files={}, manifest=dict(host_sdk=dict(houdini="22.0.466")), runtime_fingerprint="0"
        )
        report = dict(gates={})
        with self.assertRaises(PACKAGE["PackageError"]):
            PACKAGE["archive_contents"](plan, report, {}, "public", (), community())
        files, manifest = PACKAGE["archive_contents"](
            plan, report, {}, "internal", (), community(), dict(official=False)
        )
        self.assertEqual(manifest["build_identity"], community())
        self.assertEqual(manifest["build_record"]["official"], False)
        # Whatever the caller's record says.
        files, manifest = PACKAGE["archive_contents"](
            plan, report, {}, "internal", (), community(), dict(official=True, fork_policy="shipped")
        )
        self.assertIs(manifest["build_record"]["official"], False)
        with self.assertRaises(PACKAGE["PackageError"]):
            PACKAGE["archive_contents"](plan, report, {}, "internal", (), community(), ["official"])
        self.assertIn("community build", manifest["certification"])

    def test_an_active_official_build_blocks_a_community_install_and_back(self):
        # Ignore real Windows installations of this computer: only the preference folder counts here.
        scope = PACKAGE["validate_install_paths"].__globals__
        with patch.dict(scope, windows_installations=lambda: []), tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            prefs = base / "prefs"
            (prefs / "packages").mkdir(parents=True)
            official = PACKAGE["target_identity"]("22.0.466")["package_filename"]
            registration = dict(enable="houdini_version == '22.0.466'", env=[])
            (prefs / "packages" / official).write_text(json.dumps(registration))
            with self.assertRaisesRegex(PACKAGE["PackageError"], "Another Cycles package"):
                PACKAGE["validate_install_paths"](
                    base / "app/installation", prefs, base / "data", "22.0.466", base / "app", build=community()
                )
            (prefs / "packages" / official).unlink()
            mine = PACKAGE["target_identity"]("22.0.466", community())["package_filename"]
            (prefs / "packages" / mine).write_text(json.dumps(registration))
            with self.assertRaisesRegex(PACKAGE["PackageError"], "Another Cycles package"):
                PACKAGE["validate_install_paths"](
                    base / "app/installation", prefs, base / "data", "22.0.466", base / "app"
                )

    def test_community_policy_accepts_crlf_patch_copies_and_checks_the_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "patches").mkdir()
            lf = b"From: a <a@localhost>\nSubject: x\n\ndiff\n"
            (source / "patches/0001-x.patch").write_bytes(lf.replace(b"\n", b"\r\n"))  # Windows autocrlf copy
            lock = dict(
                commit="1" * 40,
                authoring_tree="2" * 40,
                patches=["patches/0001-x.patch"],
                patch_sha256={"patches/0001-x.patch": hashlib.sha256(lf).hexdigest()},
            )
            (source / "source.lock.json").write_text(json.dumps(dict(cycles=lock)))
            crlf = hashlib.sha256(lf.replace(b"\n", b"\r\n")).hexdigest()
            manifest = dict(
                engine_route="public",
                engine_tree="2" * 40,
                actual_revisions=dict(cycles="3" * 40),
                applied_patches=[dict(path="patches/0001-x.patch", sha256=crlf)],
            )
            files, reason = PACKAGE["community_policy_files"](source, manifest)
            self.assertEqual(files, {})
            self.assertIn("do not cover", reason)  # no patches/triage.json in this fixture
            for bad in (
                dict(manifest, engine_tree="4" * 40),
                dict(
                    manifest,
                    applied_patches=[
                        dict(path="patches/0001-x.patch", sha256=lock["patch_sha256"]["patches/0001-x.patch"])
                    ],
                ),
                dict(manifest, applied_patches=[]),
            ):
                with self.subTest(bad=bad), self.assertRaises(PACKAGE["PackageError"]):
                    PACKAGE["community_policy_files"](source, bad)
            # The official route keeps refusing a public-route build.
            with self.assertRaisesRegex(PACKAGE["PackageError"], "only from the private engine route"):
                PACKAGE["fork_policy_files"](source, manifest)

    def test_setup_passes_the_identity_only_for_community_builds(self):
        setup = (ROOT / "packaging/installer/CyclesHoudini.iss").read_text(encoding="utf-8")
        for name in ("AppName", "AppPublisherName", "AppPublisherLink"):
            self.assertIn("#ifndef " + name, setup)
        self.assertIn("#if defined(HasRefreshRegistration) || defined(BuildId)", setup)
        builder = (ROOT / "scripts/build-installer.ps1").read_text(encoding="utf-8")
        self.assertIn('"/DBuildId=$($target.build_id)"', builder)
        self.assertIn('"/DAppPublisherLink=$($target.publisher_url)"', builder)
        # Setup's first page lists the checks that did not run, not only those that passed.
        self.assertIn("- Checks that did not run: ", builder)


class SetupTransactionTest(unittest.TestCase):
    """Setup installs with --defer-commit and then runs commit-setup, or abort-setup on error
    (packaging/installer/CyclesHoudini.iss). Each path for the official and a community build."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        scope = PACKAGE["install"].__globals__
        self.patches = [
            patch.dict(scope, windows_installations=lambda: []),
            patch.dict(os.environ, {"ProgramFiles": str(self.base / "no-program-files")}),
        ]
        for item in self.patches:
            item.start()
        self.prefs = self.base / "prefs"
        self.archives = dict(official=self.archive("official", None), community=self.archive("community", community()))

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def archive(self, name, build):
        canonical, digest = PACKAGE["canonical"], PACKAGE["digest"]
        provider = dict(hdk_api=22000453, files=[])
        files = {
            "host/bin/ch_worker.exe": b"native fixture " + name.encode(),
            "host/dso/_cycles_houdini.dll": b"host",
            "host/resources/sdk-dependencies.json": canonical(dict(openimagedenoise=provider)),
            "runtime/bin/embree4.dll": b"dependency",
        }
        manifest = dict(
            schema_version=PACKAGE["SCHEMA"],
            platform="windows-x64",
            product="Cycles for Houdini",
            purpose="internal",
            houdini="22.0.466",
            runtime_fingerprint="a" * 64,
            audit={"build_sources": "passed", "dependencies": "passed", "notices": "passed"},
            certification="internal candidate",
            files=[dict(path=key, bytes=len(value), sha256=digest(value)) for key, value in sorted(files.items())],
        )
        if build is not None:
            manifest["build_identity"] = build
        manifest["package_id"] = digest(canonical(manifest))
        files[PACKAGE["MANIFEST"]] = canonical(manifest)
        path = self.base / (name + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            for key, value in files.items():
                archive.writestr(key, value)
        return path

    def root(self, which):
        return self.base / (which + "-app") / "installation"

    def install(self, which, **options):
        return PACKAGE["install"](
            self.archives[which],
            self.root(which),
            self.prefs,
            self.base / (which + "-data"),
            allow_internal=True,
            application_root=self.base / (which + "-app"),
            **options,
        )

    def registered(self):
        folder = self.prefs / "packages"
        return sorted(path.name for path in folder.glob("*.json")) if folder.is_dir() else []

    def journals(self, which):
        return [json.loads(path.read_text()) for path in sorted((self.root(which) / "transactions").glob("*.json"))]

    def filename(self, which):
        build = community() if which == "community" else None
        return PACKAGE["target_identity"]("22.0.466", build)["package_filename"]

    def test_setup_commit_then_refresh_and_uninstall(self):
        for which in ("official", "community"):
            with self.subTest(which=which):
                self.install(which, defer_commit=True, setup_token="setup-1")
                self.assertEqual(self.registered(), [self.filename(which)])
                self.assertEqual(PACKAGE["commit_setup"](self.root(which), "setup-1")["result"], "passed")
                self.assertEqual([item["state"] for item in self.journals(which)], ["committed"])
                self.assertEqual(PACKAGE["refresh_registration"](self.root(which))["result"], "passed")
                PACKAGE["uninstall"](self.root(which))
                self.assertEqual(self.registered(), [])
                # Only a community journal names a build identity; official journals are unchanged.
                for journal in self.journals(which):
                    self.assertEqual("build_identity" in journal, which == "community")

    def test_setup_abort_removes_the_activation(self):
        for which in ("official", "community"):
            with self.subTest(which=which):
                self.install(which, defer_commit=True, setup_token="setup-2")
                self.assertEqual(PACKAGE["abort_setup"](self.root(which), "setup-2")["result"], "passed")
                self.assertEqual(self.registered(), [])
                self.assertFalse((self.root(which) / "state.json").exists())
                # A later Setup into the same folder works.
                self.install(which, defer_commit=True, setup_token="setup-3")
                PACKAGE["commit_setup"](self.root(which), "setup-3")
                PACKAGE["uninstall"](self.root(which))

    def test_interrupted_setup_is_recovered(self):
        for which in ("official", "community"):
            with self.subTest(which=which):
                self.install(which, defer_commit=True, setup_token="setup-4")
                with self.assertRaisesRegex(PACKAGE["PackageError"], "Recover the interrupted"):
                    PACKAGE["uninstall"](self.root(which))
                self.assertEqual(len(PACKAGE["recover"](self.root(which))["recovered"]), 1)
                self.assertEqual(self.registered(), [])
                self.install(which)
                self.assertEqual(PACKAGE["refresh_registration"](self.root(which))["result"], "passed")
                PACKAGE["uninstall"](self.root(which))
                self.assertEqual(self.registered(), [])

    def test_community_over_official_and_official_over_community(self):
        for first, second in (("official", "community"), ("community", "official")):
            with self.subTest(first=first, second=second):
                self.install(first, defer_commit=True, setup_token="a")
                PACKAGE["commit_setup"](self.root(first), "a")
                with self.assertRaisesRegex(PACKAGE["PackageError"], "Another Cycles package"):
                    self.install(second, defer_commit=True, setup_token="b")
                self.assertEqual(self.registered(), [self.filename(first)])
                PACKAGE["uninstall"](self.root(first))
                self.install(second, defer_commit=True, setup_token="b")
                PACKAGE["commit_setup"](self.root(second), "b")
                self.assertEqual(self.registered(), [self.filename(second)])
                PACKAGE["uninstall"](self.root(second))
                self.assertEqual(self.registered(), [])

    def legacy_official(self):
        """An old official installation registered under the legacy name cycles_houdini_dev.json."""
        self.install("official")
        root = self.root("official")
        state = json.loads((root / "state.json").read_text())
        generic = self.prefs / "packages" / PACKAGE["PACKAGE_FILE"]
        Path(state["package_path"]).replace(generic)
        for name in ("houdini", "package_filename", "app_id"):
            state.pop(name)
        state.update(package_path=str(generic))
        PACKAGE["atomic_json"](root / "state.json", state)
        marker_path = root / PACKAGE["INSTALL_MARKER"]
        marker = json.loads(marker_path.read_text())
        for name in PACKAGE["target_identity"]("22.0.466"):
            marker.pop(name, None)
        PACKAGE["atomic_json"](marker_path, marker)
        return generic, root / "state.json"

    def test_community_refuses_an_old_official_dev_registration(self):
        generic, state = self.legacy_official()
        before = generic.read_bytes(), state.read_bytes()
        for options in (dict(), dict(defer_commit=True, setup_token="e")):
            with self.subTest(options=options), self.assertRaises(PACKAGE["PackageError"]):
                self.install("community", **options)
            self.assertEqual((generic.read_bytes(), state.read_bytes()), before)
        # The install tool refuses on its own too, even if the shared-preferences check were passed.
        scope = PACKAGE["install"].__globals__
        with patch.dict(scope, validate_install_paths=lambda *arguments, **options: None):
            with self.assertRaisesRegex(PACKAGE["PackageError"], "older official"):
                self.install("community", defer_commit=True, setup_token="f")
            with self.assertRaisesRegex(PACKAGE["PackageError"], "older official"):
                PACKAGE["registration_status"](
                    self.root("community"),
                    self.prefs,
                    self.base / "community-data",
                    "22.0.466",
                    self.base / "community-app",
                    archive=self.archives["community"],
                    build_id="community",
                )
        self.assertEqual((generic.read_bytes(), state.read_bytes()), before)
        self.assertFalse((self.root("community") / "state.json").exists())

    def test_a_kept_folder_of_one_build_is_refused_by_the_other(self):
        for first, second in (("official", "community"), ("community", "official")):
            with self.subTest(first=first, second=second):
                self.install(first)
                PACKAGE["uninstall"](self.root(first))  # the folder and its marker stay, by design
                marker = (self.root(first) / PACKAGE["INSTALL_MARKER"]).read_bytes()
                with self.assertRaisesRegex(PACKAGE["PackageError"], "belongs to another Cycles for Houdini build"):
                    PACKAGE["install"](
                        self.archives[second],
                        self.root(first),
                        self.prefs,
                        self.base / (first + "-data"),
                        allow_internal=True,
                        application_root=self.base / (first + "-app"),
                    )
                build = community() if second == "community" else None
                with self.assertRaisesRegex(PACKAGE["PackageError"], "belongs to another Cycles for Houdini build"):
                    PACKAGE["registration_status"](
                        self.root(first),
                        self.prefs,
                        self.base / (first + "-data"),
                        "22.0.466",
                        self.base / (first + "-app"),
                        archive=self.archives[second],
                        build_id=(build or {}).get("build_id"),
                    )
                self.assertEqual((self.root(first) / PACKAGE["INSTALL_MARKER"]).read_bytes(), marker)
                self.assertEqual(self.registered(), [])
                # The same build may come back into its own folder.
                self.install(first)
                PACKAGE["uninstall"](self.root(first))

    def test_journal_identity_is_checked(self):
        self.install("community", defer_commit=True, setup_token="c")
        (path,) = sorted((self.root("community") / "transactions").glob("*.json"))
        journal = json.loads(path.read_text())
        # A journal written before it carried the identity (the first community Setups) is still understood.
        path.write_text(json.dumps({key: value for key, value in journal.items() if key != "build_identity"}))
        PACKAGE["commit_setup"](self.root("community"), "c")
        PACKAGE["uninstall"](self.root("community"))
        self.install("community", defer_commit=True, setup_token="d")
        (path,) = [
            item
            for item in sorted((self.root("community") / "transactions").glob("*.json"))
            if json.loads(item.read_text())["state"] == "setup_pending"
        ]
        journal = json.loads(path.read_text())
        path.write_text(json.dumps(dict(journal, build_identity=community(build_id="someone-else"))))
        for action in (
            lambda: PACKAGE["commit_setup"](self.root("community"), "d"),
            lambda: PACKAGE["abort_setup"](self.root("community"), "d"),
        ):
            with self.assertRaises(PACKAGE["PackageError"]):
                action()
        path.write_text(json.dumps(journal))
        PACKAGE["abort_setup"](self.root("community"), "d")
        self.assertEqual(self.registered(), [])


class EngineEditLoopTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(
            os.environ,
            dict(
                GIT_AUTHOR_NAME="fixture",
                GIT_AUTHOR_EMAIL="fixture@example.invalid",
                GIT_COMMITTER_NAME="fixture",
                GIT_COMMITTER_EMAIL="fixture@example.invalid",
                GIT_CONFIG_GLOBAL=os.devnull,
            ),
        )
        env.start()
        self.addCleanup(env.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base_dir = Path(temporary.name)
        upstream = self.base_dir / "upstream"
        upstream.mkdir()
        git(upstream, "init", "-q")
        (upstream / "src").mkdir()
        (upstream / "src/a.cpp").write_bytes(b"one\ntwo\n")
        git(upstream, "add", ".")
        git(upstream, "commit", "-qm", "upstream")
        base = git(upstream, "rev-parse", "HEAD")
        (upstream / "src/a.cpp").write_bytes(b"one\ntwo\nthree\n")
        git(upstream, "commit", "-qam", "First locked patch")
        patch_bytes = subprocess.check_output(
            ["git", "-C", str(upstream), "format-patch", "--stdout", "--full-index", "--binary", "-1"]
        )
        tree = git(upstream, "rev-parse", "HEAD^{tree}")
        git(upstream, "reset", "-q", "--hard", base)
        self.root = self.base_dir / "repo"
        (self.root / "patches").mkdir(parents=True)
        (self.root / "patches/0001-first.patch").write_bytes(patch_bytes)
        lock = dict(
            cycles=dict(
                url=upstream.as_uri(),
                commit=base,
                authoring_tip="0" * 40,
                authoring_tree=tree,
                patches=["patches/0001-first.patch"],
                patch_sha256={"patches/0001-first.patch": hashlib.sha256(patch_bytes).hexdigest()},
            )
        )
        (self.root / "source.lock.json").write_bytes(
            json.dumps(lock, indent=2).replace("\n", "\r\n").encode() + b"\r\n"
        )
        (self.root / "build-identity.json").write_text(json.dumps(community(publisher="Test Builder")))
        self.engine = self.root / "third_party/cycles"
        FORK["materialize_public"](self.root, self.engine, self.base_dir / "replay-0")

    def lock(self):
        return json.loads((self.root / "source.lock.json").read_text(encoding="utf-8-sig"))["cycles"]

    def test_edit_export_and_fresh_replay_give_the_same_engine(self):
        # A Windows editor saves an LF file with CRLF; the tool stores it as LF.
        (self.engine / "src/a.cpp").write_bytes(b"one\r\ntwo\r\nthree\r\nfour\r\n")
        (self.engine / "src/new.h").write_bytes(b"new\n")
        result = UPDATE["update"](self.root, "Add four", True, False, False)
        self.assertEqual(result["result"], "passed")
        self.assertEqual([item["path"] for item in result["added"]], ["patches/0002-Add-four.patch"])
        self.assertEqual((self.engine / "src/a.cpp").read_bytes(), b"one\ntwo\nthree\nfour\n")
        lock = self.lock()
        self.assertEqual(lock["patches"][-1], "patches/0002-Add-four.patch")
        self.assertEqual(lock["authoring_tree"], git(self.engine, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(lock["authoring_tip"], "0" * 40)  # the private fork's tip is left alone
        self.assertTrue((self.root / "source.lock.json").read_bytes().count(b"\r\n") > 3)  # CRLF kept
        self.assertIn("Test Builder <engine-edit@localhost>", (self.root / lock["patches"][-1]).read_text())
        self.assertEqual(IDENTITY["engine_route"](self.engine, lock), "public")
        self.assertEqual(FORK["verify"](self.root, self.engine, self.base_dir / "replay-1")["result"], "passed")
        # A fresh clone (same patches/ and lock, new folder) rebuilds the same commits.
        fresh = self.base_dir / "fresh"
        (fresh / "patches").mkdir(parents=True)
        for name in lock["patches"]:
            (fresh / name).write_bytes((self.root / name).read_bytes())
        (fresh / "source.lock.json").write_bytes((self.root / "source.lock.json").read_bytes())
        FORK["materialize_public"](fresh, fresh / "third_party/cycles", self.base_dir / "replay-2")
        self.assertEqual(git(fresh / "third_party/cycles", "rev-parse", "HEAD"), git(self.engine, "rev-parse", "HEAD"))
        # Running it again changes nothing.
        self.assertEqual(UPDATE["update"](self.root, None, True, False, False)["result"], "unchanged")

    def test_own_commit_is_rewritten_to_the_fresh_clone_commit(self):
        (self.engine / "src/a.cpp").write_bytes(b"one\ntwo\nthree\nmine\n")
        git(self.engine, "add", ".")
        with patch.dict(os.environ, GIT_COMMITTER_DATE="2030-01-01T00:00:00Z"):
            git(self.engine, "commit", "-qm", "My own commit")
        own = git(self.engine, "rev-parse", "HEAD")
        result = UPDATE["update"](self.root, None, True, False, False)
        self.assertEqual(result["previous_engine_head"], own)
        self.assertNotEqual(result["engine_head"], own)
        self.assertEqual(git(self.engine, "rev-parse", "HEAD"), result["engine_head"])
        self.assertEqual(git(self.engine, "status", "--porcelain"), "")

    def test_refusals_change_nothing(self):
        before = (self.root / "source.lock.json").read_bytes()
        (self.engine / "lib/windows_x64").mkdir(parents=True)
        (self.engine / "lib/windows_x64/x.txt").write_text("dependency")
        with self.assertRaisesRegex(UPDATE["Stop"], "lib/windows_x64"):
            UPDATE["update"](self.root, None, True, False, False)
        (self.engine / "lib/windows_x64/x.txt").unlink()
        with self.assertRaisesRegex(UPDATE["Stop"], "Nothing was changed"):
            # Windows line endings: answering "no" must leave even them untouched.
            (self.engine / "src/a.cpp").write_bytes(b"declined\r\n")
            UPDATE["update"](self.root, None, False, False, False)
        self.assertEqual((self.engine / "src/a.cpp").read_bytes(), b"declined\r\n")
        self.assertEqual((self.root / "source.lock.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.root / "patches").iterdir()), ["0001-first.patch"])

    def test_patch_dates_are_utc(self):
        (self.engine / "src/a.cpp").write_bytes(b"one\ntwo\nthree\nlocal\n")
        git(self.engine, "add", ".")
        with patch.dict(
            os.environ, GIT_AUTHOR_DATE="2030-01-01T12:00:00+05:30", GIT_COMMITTER_DATE="2030-01-01T12:00:00+05:30"
        ):
            git(self.engine, "commit", "-qm", "Made in another time zone")
        (self.engine / "src/b.cpp").write_bytes(b"tool commit\n")
        result = UPDATE["update"](self.root, "Tool commit", True, False, False)
        first, second = ((self.root / item["path"]).read_text() for item in result["added"])
        self.assertIn("\nDate: Tue, 01 Jan 2030 06:30:00 +0000\n", first)
        self.assertNotIn("+0530", first)
        self.assertRegex(second, r"\nDate: [^\n]* \+0000\n")
        self.assertEqual(FORK["verify"](self.root, self.engine, self.base_dir / "replay-utc")["result"], "passed")

    def test_yes_does_not_publish_an_e_mail_address(self):
        (self.engine / "src/a.cpp").write_bytes(b"one\ntwo\nthree\nmine\n")
        git(self.engine, "add", ".")
        address = "someone" + "@" + "mail.test"  # built here, so the source holds no e-mail address
        with patch.dict(os.environ, GIT_AUTHOR_EMAIL=address):
            git(self.engine, "commit", "-qm", "Own address")
        before = (self.root / "source.lock.json").read_bytes()

        def nobody(prompt=""):
            raise EOFError  # no person at the keyboard

        with patch("builtins.input", nobody):
            with self.assertRaisesRegex(UPDATE["Stop"], "Nothing was exported"):
                UPDATE["update"](self.root, None, True, False, True)
        self.assertEqual((self.root / "source.lock.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.root / "patches").iterdir()), ["0001-first.patch"])

    def test_an_interrupted_run_is_continued(self):
        (self.engine / "src/a.cpp").write_bytes(b"one\ntwo\nthree\nfour\n")
        scope = UPDATE["update"].__globals__

        def crash(*arguments):
            raise KeyboardInterrupt("stopped between the patch files and the lock")

        with patch.dict(scope, write_lock=crash), self.assertRaises(KeyboardInterrupt):
            UPDATE["update"](self.root, "Add four", True, False, False)
        self.assertTrue((self.root / "patches/0002-Add-four.patch").exists())
        self.assertEqual(self.lock()["patches"], ["patches/0001-first.patch"])
        # A stray patch file of another change stops the run, changing nothing.
        stray = self.root / "patches/0003-other.patch"
        stray.write_bytes(b"From: x <x@localhost>\n\nother\n")
        with self.assertRaisesRegex(UPDATE["Stop"], "not listed in source.lock.json"):
            UPDATE["update"](self.root, None, True, False, False)
        stray.unlink()
        result = UPDATE["update"](self.root, None, True, False, False)
        self.assertEqual([item["path"] for item in result["added"]], ["patches/0002-Add-four.patch"])
        self.assertEqual(self.lock()["patches"][-1], "patches/0002-Add-four.patch")
        self.assertEqual(FORK["verify"](self.root, self.engine, self.base_dir / "replay-resume")["result"], "passed")

    def test_saved_but_uncommitted_records_are_committed_by_the_next_run(self):
        git(self.root, "init", "-q")
        (self.root / ".gitignore").write_text("third_party/\n")
        git(self.root, "add", ".")
        git(self.root, "commit", "-qm", "fixture")
        (self.engine / "src/a.cpp").write_bytes(b"one\ntwo\nthree\nfour\n")
        UPDATE["update"](self.root, "Add four", True, False, False)  # answered "no" to the commit
        self.assertIn("source.lock.json", git(self.root, "status", "--porcelain"))
        result = UPDATE["update"](self.root, None, True, False, None)
        self.assertEqual(result["result"], "unchanged")
        self.assertTrue(result["committed"])
        self.assertEqual(git(self.root, "status", "--porcelain"), "")


# The pins cover the official Inno defines and the installer builder for a package without
# build_identity. They include the Windows maintenance path for interrupted Setup and uninstall.
# Review any changes to the effective official scripts before updating their digests.
def ispp(text, defines):
    """The Inno Setup preprocessor directives CyclesHoudini.iss uses, nothing more."""
    names = dict(defines)

    def value(expression):
        parts = [part.strip() for part in expression.split("+")]
        return "".join(part[1:-1] if part.startswith('"') else names[part] for part in parts)

    def condition(expression):
        expression = re.sub(r"defined\((\w+)\)", lambda m: str(m[1] in names), expression)
        expression = re.sub(
            r"\b(?!True\b|False\b)([A-Za-z_]\w*)\b(?=\s*[!=]=)", lambda m: repr(names[m[1]]), expression
        )
        expression = expression.replace("||", " or ").replace("&&", " and ")
        assert re.fullmatch(r"[\sA-Za-z'\"().:/!=-]*", expression), expression
        return bool(eval(expression, {"__builtins__": {}}))

    out, stack, active = [], [], True
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            key, _, rest = stripped[1:].partition(" ")
            rest = rest.strip()
            if key in ("ifdef", "ifndef", "if"):
                taken = (
                    (rest in names) if key == "ifdef" else (rest not in names) if key == "ifndef" else condition(rest)
                )
                stack.append((active, taken))
                active = active and taken
            elif key == "else":
                parent, taken = stack[-1]
                stack[-1] = (parent, not taken)
                active = parent and not taken
            elif key == "endif":
                active = stack.pop()[0]
            elif key == "define" and active:
                name, _, expression = rest.partition(" ")
                names[name] = value(expression.strip())
            elif key == "error" and active:
                raise RuntimeError(rest)
            elif key not in ("define", "error"):
                raise ValueError("unknown directive " + stripped)
            continue
        if active and not stripped.startswith(";"):
            out.append(re.sub(r"\{#(\w+)\}", lambda m: names.get(m[1], m[0]), line))
    assert not stack
    return "\n".join(out)


OFFICIAL = dict(
    PackageZip="p.zip",
    PackageZipName="p.zip",
    PackageSha="0" * 64,
    StageDir="stage",
    OutputDir="out",
    OutputName="Setup",
    AppVersion="2026.11.21 (00000000)",
    VersionInfo="2026.11.21.0",
    HoudiniVersion="22.0.466",
    HoudiniPrefVersion="22.0",
    TargetAppId="{B451136F-8173-5CC3-9C60-2A37259A915F}",
    TargetDirectory="Cycles for Houdini 22.0.466",
    TargetPackageFile="cycles_houdini_22_0_466.json",
)


def ps_code(text):
    return "\n".join(line for line in text.replace("\r\n", "\n").split("\n") if not line.lstrip().startswith("#"))


def official_installer_text(text):
    """build-installer.ps1 as it runs for an official package (no build_identity in the manifest)."""
    lines, out, skipping, keep_else = ps_code(text).split("\n"), [], False, False
    for line in lines:
        if line == "if($community){":
            skipping, keep_else = True, False
            continue
        if skipping:
            if line == "}else{":
                keep_else = True
            elif line == "}":
                skipping = keep_else = False
            elif keep_else:
                out.append(line[4:])
            continue
        if line in ("$communityText=''",) or line.startswith("$community=$manifest"):
            continue
        line = line.replace("$(if($community){$target.display_name}else{'Cycles for Houdini'})", "Cycles for Houdini")
        out.append("" if line == "$communityText" else line.replace(" -Depth 8", ""))
    return "\n".join(out)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


class CudaToolkitPartsTest(unittest.TestCase):
    """scripts/cuda-toolkit-parts.py: only checked ZIP archives, unpacked into one folder, never a driver."""

    def setUp(self):
        self.tool = runpy.run_path(str(ROOT / "scripts/cuda-toolkit-parts.py"))
        self.temp = Path(tempfile.mkdtemp(prefix="cuda-parts-"))
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.mirror = self.temp / "mirror"
        contents = {
            "cuda_nvcc": {"bin/nvcc.exe": b"nvcc", "bin/nvcc.profile": b"profile", "include/crt/host_config.h": b"h"},
            "cuda_cudart": {"include/cuda.h": b"h", "include/cuda_runtime.h": b"h", "lib/x64/cudart.lib": b"lib"},
            "cuda_cccl": {"include/nv/target": b"h"},
        }
        self.parts = []
        for part in self.tool["PARTS"]:
            self.parts.append(self.make_archive(part, contents[part["name"]]))
        self.use(self.parts)

    def make_archive(self, part, files):
        archive = self.mirror / part["path"]
        archive.parent.mkdir(parents=True, exist_ok=True)
        top = Path(part["path"]).stem
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(top + "/LICENSE", "CUDA EULA")
            for name, data in files.items():
                bundle.writestr(top + "/" + name, data)
        data = archive.read_bytes()
        return dict(part, sha256=hashlib.sha256(data).hexdigest(), size=len(data))

    def use(self, parts):
        self.tool["get"].__globals__["PARTS"] = tuple(parts)

    def get(self, root):
        return self.tool["get"](root, source=str(self.mirror), log=lambda text: None)

    def test_parts_are_the_documented_toolkit_parts_only(self):
        names = [part["name"] for part in self.tool["PARTS"]]
        self.assertEqual(names, ["cuda_nvcc", "cuda_cudart", "cuda_cccl"])
        for part in self.tool["PARTS"]:
            self.assertTrue(part["path"].startswith(part["name"] + "/windows-x86_64/"))
            self.assertTrue(part["path"].endswith("-archive.zip"))
            self.assertRegex(part["sha256"], "^[0-9a-f]{64}$")
        self.assertEqual(
            self.tool["LOCK_TOOLKIT"],
            json.loads((ROOT / "source.lock.json").read_text(encoding="utf-8"))["certification_candidate"][
                "cuda_toolkit"
            ],
        )
        self.assertTrue(self.tool["VERSION"].startswith(self.tool["LOCK_TOOLKIT"] + "."))
        text = (ROOT / "scripts/cuda-toolkit-parts.py").read_text(encoding="utf-8")
        for never in ("subprocess", "os.system", "startfile", "Popen", "winreg"):
            self.assertNotIn(never, text)  # it runs nothing and changes no setting

    class FakeResponse(io.BytesIO):
        def __init__(self, data, status):
            super().__init__(data)
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def fake_opener(self, data, failures, cut_after=None, honour_range=True):
        """Fails `failures` times (a timeout, then a reset connection, ...), optionally after sending part of the
        file, then serves it (from the requested byte when it honours Range)."""
        calls = []

        def opener(request, timeout=None):
            calls.append(request.get_header("Range"))
            if len(calls) <= failures:
                if cut_after is None:
                    raise TimeoutError("[WinError 10060] the server did not answer")
                start = int(request.get_header("Range")[6:-1]) if request.get_header("Range") and honour_range else 0

                class Cut(self.FakeResponse):
                    def read(inner, n=-1):
                        if inner.tell() >= cut_after:
                            raise ConnectionResetError("connection reset")
                        return super().read(min(n, cut_after - inner.tell()) if n > 0 else cut_after - inner.tell())

                return Cut(data[start:], 206 if start else 200)
            rng = request.get_header("Range")
            if rng and honour_range:
                return self.FakeResponse(data[int(rng[6:-1]) :], 206)
            return self.FakeResponse(data, 200)

        return opener, calls

    def download(self, opener, data):
        pauses, lines = [], []
        target = self.temp / "dl" / "part.zip"
        target.parent.mkdir(exist_ok=True)
        self.tool["_download"](
            "https://example.invalid/part.zip",
            target,
            len(data),
            hashlib.sha256(data).hexdigest(),
            lines.append,
            opener=opener,
            sleep=pauses.append,
        )
        return target, pauses, lines

    def test_download_retries_with_growing_pauses_and_continues_a_partial_file(self):
        data = bytes(range(256)) * 4096  # 1 MiB
        opener, calls = self.fake_opener(data, failures=2)
        target, pauses, _ = self.download(opener, data)
        self.assertEqual((target.read_bytes() == data, pauses, len(calls)), (True, [2.0, 4.0], 3))
        self.assertFalse(target.with_name("part.zip.partial").exists())
        # A connection that breaks after part of the file: the next try asks for the rest (Range) and appends it.
        target.unlink()
        opener, calls = self.fake_opener(data, failures=2, cut_after=300_000)
        target, pauses, _ = self.download(opener, data)
        self.assertEqual(target.read_bytes(), data)
        self.assertEqual(calls[0], None)
        self.assertTrue(calls[1].startswith("bytes=300000-"))
        # A server that ignores Range: the file is started again, never glued together wrongly.
        target.unlink()
        opener, calls = self.fake_opener(data, failures=1, cut_after=300_000, honour_range=False)
        target, _, lines = self.download(opener, data)
        self.assertEqual(target.read_bytes(), data)
        self.assertIn("      starting this file again", lines)

    def test_download_that_never_answers_stops_with_one_plain_sentence(self):
        data = b"x" * 1000
        opener, calls = self.fake_opener(data, failures=99)
        with self.assertRaises(self.tool["DownloadError"]) as stopped:
            self.download(opener, data)
        self.assertEqual(len(calls), self.tool["ATTEMPTS"])
        message = str(stopped.exception)
        self.assertIn("did not answer", message)
        self.assertIn("double-click the file again", message)
        self.assertEqual(message.count(". "), 0)  # one sentence
        # A complete file that is not what the manifest says is deleted and downloaded again; never used.
        wrong = b"y" * 1000
        opener, calls = self.fake_opener(wrong, failures=0)
        with self.assertRaises(self.tool["DownloadError"]):
            self.download(opener, data)
        self.assertFalse((self.temp / "dl" / "part.zip").exists())

    def test_get_unpacks_into_one_folder_and_is_safe_to_repeat(self):
        root = self.temp / "tools" / "cuda-test"
        result = self.get(root)
        self.assertTrue(result["complete"], result)
        for name in self.tool["REQUIRED_FILES"]:
            self.assertTrue((root / name).is_file(), name)
        self.assertTrue((root / "include/nv/target").is_file())
        self.assertFalse((root / "cuda_nvcc-windows-x86_64-12.8.61-archive").exists())
        self.assertEqual(sorted(p.name for p in root.parent.iterdir()), ["cuda-test"])  # downloads and staging removed
        self.assertTrue(self.get(root)["complete"])  # second run: nothing to do
        (root / "lib/x64/cudart.lib").unlink()
        self.assertEqual(self.tool["status"](root)["missing"], ["lib/x64/cudart.lib"])

    def test_wrong_archive_driver_files_and_escapes_are_refused(self):
        bad = dict(self.parts[1], sha256="0" * 64)
        self.use([self.parts[0], bad, self.parts[2]])
        with self.assertRaisesRegex(self.tool["PartsError"], "SHA-256"):
            self.get(self.temp / "a")
        self.assertFalse((self.temp / "a").exists())
        for files, message in (
            ({"include/cuda.h": b"h", "display/nvlddmkm.sys": b"x"}, "driver file"),
            ({"include/cuda.h": b"h", "display/nv_disp.inf": b"x"}, "driver file"),
            ({"../../escape.h": b"x"}, "outside"),
        ):
            evil = self.make_archive(self.tool["PARTS"][1], files)
            self.use([self.parts[0], evil, self.parts[2]])
            with self.assertRaisesRegex(self.tool["PartsError"], message):
                self.get(self.temp / "b")
            self.assertFalse((self.temp / "b").exists())
            self.assertFalse(any(p.name.startswith("b.incomplete") for p in self.temp.iterdir()))
        self.assertFalse((self.temp.parent / "escape.h").exists())

    def test_a_foreign_folder_is_never_overwritten(self):
        root = self.temp / "mine"
        root.mkdir()
        (root / "notes.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(self.tool["PartsError"], "not a complete copy"):
            self.get(root)
        self.assertEqual((root / "notes.txt").read_text(encoding="utf-8"), "keep")


class OfficialRoutePinTest(unittest.TestCase):
    def test_official_setup_matches_reviewed_script(self):
        setup = (ROOT / "packaging/installer/CyclesHoudini.iss").read_text(encoding="utf-8")
        self.assertEqual(
            digest(ispp(setup, OFFICIAL)), "a4da2d3ffbcbdf3f3fd54374e7994e34490c97672849c41a06617be767454e50"
        )
        self.assertEqual(
            digest(ispp(setup, dict(OFFICIAL, HasRefreshRegistration="1"))),
            "77ba5e610ef8245d1985b72fe50743bc86c772b63219cdd2308273a4eaff0b54",
        )
        # The emulation sees the community defines (so the pins above are not blind).
        mine = PACKAGE["target_identity"]("22.0.466", community(build_id="anna-hair-fix"))
        text = ispp(
            setup,
            dict(
                OFFICIAL,
                AppName=mine["display_name"],
                AppPublisherName="Anna",
                AppPublisherLink="",
                BuildId="anna-hair-fix",
            ),
        )
        self.assertIn("AppName=Cycles for Houdini - community build", text)
        self.assertNotIn("AppPublisherURL=", text)
        self.assertIn("--build-id anna-hair-fix", text)
        fork_url = "https://github.com/anna/Continuous-Cycles"
        mine = PACKAGE["target_identity"]("22.0.466", community(publisher="Anna", publisher_url=fork_url))
        text = ispp(
            setup,
            dict(
                OFFICIAL,
                AppName=mine["display_name"],
                AppPublisherName=mine["publisher"],
                AppPublisherLink=mine["publisher_url"],
                BuildId=mine["build_id"],
            ),
        )
        self.assertIn(f"AppPublisherURL={fork_url}\n", text)

    def test_official_installer_builder_matches_reviewed_script(self):
        builder = (ROOT / "scripts/build-installer.ps1").read_text(encoding="utf-8")
        self.assertEqual(
            digest(official_installer_text(builder)),
            "8967c30e5d2f4682d91ed5a03aa499c4c1595c9248f3dd003d900378f40a55f4",
        )

    def test_official_setup_recovers_and_uninstalls_with_windows_powershell(self):
        setup = (ROOT / "packaging/installer/CyclesHoudini.iss").read_text(encoding="utf-8")
        for defines in (OFFICIAL, dict(OFFICIAL, HasRefreshRegistration="1")):
            with self.subTest(refresh="HasRefreshRegistration" in defines):
                text = ispp(setup, defines)
                self.assertIn("AppName=Cycles for Houdini\n", text)
                self.assertIn("AppPublisher=The Cycles for Houdini Authors\n", text)
                self.assertIn(f"AppPublisherURL={PACKAGE['OFFICIAL_PUBLISHER_URL']}\n", text)
                self.assertIn(r'Source: "stage\maintain-registration.ps1"; DestDir: "{app}\tools"', text)
                self.assertIn(r"{sys}\WindowsPowerShell\v1.0\powershell.exe", text)
                self.assertIn("-WizardLeaseHeld", text)
                prepare = text.split("function PrepareToInstall", 1)[1].split("procedure CurStepChanged", 1)[0]
                marker = prepare.index("FileExists(AddBackslash(Root) + '.cycles-houdini-installation.json')")
                extraction = prepare.index("ExtractTemporaryFile('maintain-registration.ps1')")
                recover = prepare.index(r"RunMaintenance('Recover', ExpandConstant('{tmp}\maintain-registration.ps1')")
                verify = prepare.index("registration-status --houdini")
                self.assertLess(marker, extraction)
                self.assertLess(extraction, recover)
                self.assertLess(recover, verify)
                self.assertIn("Setup stopped while recovering", prepare[recover:verify])
                uninstall = text.split("procedure CurUninstallStepChanged", 1)[1]
                self.assertIn("if CurUninstallStep <> usUninstall then", uninstall)
                self.assertIn(
                    r"RunMaintenance('Uninstall', ExpandConstant('{app}\tools\maintain-registration.ps1')", uninstall
                )
                self.assertIn("ReleaseInstallerLease", uninstall)

    def test_official_release_keeps_engine_probe_and_cached_community_dlss_guard(self):
        lines = ps_code((ROOT / "scripts/build-release.ps1").read_text(encoding="utf-8")).split("\n")
        start = lines.index("$target=$lock.certification_candidate") + 1
        self.assertEqual(
            lines[start : start + 2],
            [
                "$engineRoute=& python -B (Join-Path $PSScriptRoot 'engine-fork.py') route 2>$null",
                "$engineRouteResult=$LASTEXITCODE",
            ],
        )
        self.assertEqual(sum(line.count("engine-fork.py") for line in lines), 1)
        self.assertTrue(any(line.startswith("    if($oldCache['CH_DLSS_PROJECT_ID']){throw ") for line in lines))

    def test_official_release_rejects_public_checkouts_and_unconfirmed_plans_early(self):
        shells = [shell for shell in powershells() if Path(shell).stem.casefold() == "pwsh"]
        if not shells:
            self.skipTest("PowerShell 7 is needed for the official route")
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            scripts = source / "scripts"
            scripts.mkdir()
            for name in ("build-release.ps1", "build-layout.ps1"):
                shutil.copyfile(ROOT / "scripts" / name, scripts / name)
            shutil.copyfile(ROOT / "source.lock.json", source / "source.lock.json")
            build, output = source / "unexpected build", source / "unexpected output"
            command = [
                shells[0],
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "build-release.ps1"),
                "-HoudiniRoot",
                str(source / "missing SDK"),
                "-BuildDirectory",
                str(build),
                "-OutputDirectory",
                str(output),
            ]
            public = "START HERE - Build Cycles for Houdini.cmd"
            unknown = "The official release plan requires the locked private engine checkout."
            for route, code, dry_run, expected in (
                ("public", 0, False, public),
                ("public", 0, True, public),
                ("public", 1, True, unknown),
                ("", 1, True, unknown),
                ("unexpected", 0, True, unknown),
            ):
                with self.subTest(route=route, code=code, dry_run=dry_run):
                    (scripts / "engine-fork.py").write_text(
                        f"print({route!r})\nraise SystemExit({code})\n", encoding="utf-8"
                    )
                    result = subprocess.run(
                        [*command, *(["-DryRun"] if dry_run else [])], capture_output=True, text=True, timeout=20
                    )
                    self.assertNotEqual(result.returncode, 0)
                    message = re.sub(r"\n\s*\|\s*", " ", result.stderr)
                    self.assertIn(expected, message)
                    self.assertFalse(build.exists())
                    self.assertFalse(output.exists())

    def test_official_build_passes_no_dlss_project_id(self):
        lines = ps_code((ROOT / "scripts/build.ps1").read_text(encoding="utf-8")).split("\n")
        self.assertIn("    [string]$DlssProjectId,", lines)  # no default: absent unless a caller passes it
        uses = [line for line in lines if "DlssProjectId" in line or "CH_DLSS_PROJECT_ID" in line]
        self.assertEqual(
            uses,
            [
                "    [string]$DlssProjectId,",
                "if($DlssProjectId){$argsConfigure += \"-DCH_DLSS_PROJECT_ID=$(if($DlssProjectId -eq 'none'){''}else"
                '{$DlssProjectId})"}',
            ],
        )
        # dev-worktree.ps1 and publish-reviewed-build.ps1 are maintainer tools that are not published; they are
        # checked where they exist.
        for script in ("build-installer.ps1", "dev-worktree.ps1", "publish-reviewed-build.ps1"):
            path = ROOT / "scripts" / script
            if path.is_file():
                self.assertNotIn("DlssProjectId", path.read_text(encoding="utf-8"), script)


# The PowerShell test of scripts/easy-houdini.ps1 (made-up Houdini folders, preferences and
# build folders in a temporary directory). Run in every PowerShell found: on Windows, Windows
# PowerShell 5.1 and PowerShell 7; skipped where there is none.
EASY_HOUDINI_TEST = r"""
# Unit test of scripts/easy-houdini.ps1 with made-up Houdini folders (no real Houdini is read).
param([string]$Repo, [string]$Work)
$ErrorActionPreference = 'Stop'
$failures = New-Object System.Collections.Generic.List[string]
function Check([bool]$Condition, [string]$What) { if (-not $Condition) { $failures.Add($What) } }
function New-Houdini([string]$Base, [string]$Folder, [string]$Kit, [switch]$Binary) {
    $root = Join-Path $Base $Folder
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    if ($Kit) {
        New-Item -ItemType Directory -Path (Join-Path $root 'toolkit\include\SYS') -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $root 'toolkit\include\SYS\SYS_Version.h') -Value "#pragma once`n#define SYS_VERSION_FULL `"$Kit`"`n" -Encoding ascii
    }
    if ($Binary) { New-Item -ItemType Directory -Path (Join-Path $root 'bin') -Force | Out-Null; Set-Content -LiteralPath (Join-Path $root 'bin\houdini.exe') -Value 'x' }
    return $root
}
$env:CYCLES_EASY_TARGET_HOUDINI = $null; $env:CYCLES_EASY_TARGET_ROOT = $null; $env:CYCLES_EASY_HOUDINI = $null
# The person's own Houdini settings, as in a window opened from Houdini: the builder must drop them for its own
# processes (removed, not left defined and empty) and remember the preferences folder.
$env:HOUDINI_PATH = 'C:/my/hdas'; $env:HOUDINI_PACKAGE_DIR = 'C:/my/packages'; $env:HFS = 'C:/Houdini'
$env:HOUDINI_USER_PREF_DIR = 'C:/my/prefs/houdini__HVER__'; $env:CYCLES_EASY_HOUDINI_PREFS = $null
. (Join-Path $Repo 'scripts\easy-common.ps1')
foreach ($name in 'HOUDINI_PATH', 'HOUDINI_PACKAGE_DIR', 'HFS', 'HOUDINI_USER_PREF_DIR') { Check (-not (Test-Path -LiteralPath ('Env:' + $name))) "$name removed for the builder's processes" }
Check ($env:CYCLES_EASY_HOUDINI_PREFS -eq 'C:/my/prefs/houdini__HVER__') 'the preferences folder is remembered'
$env:CYCLES_EASY_HOUDINI_PREFS = $null
Check ((Get-EasyTestedHoudini) -eq '22.0.466') 'tested build is 22.0.466'
Check ((Get-EasySupportedHoudini) -eq '22.0') 'supported major.minor is 22.0'

# --- Detection: kit, no kit, other major.minor, not Houdini; newest first ---------------------------
$sfx = Join-Path $Work 'Program Files\Side Effects Software'
$h508 = New-Houdini $sfx 'Houdini 22.0.508' '22.0.508'
$h466 = New-Houdini $sfx 'Houdini 22.0.466' '22.0.466'
$h429 = New-Houdini $sfx 'Houdini 22.0.429' '' -Binary
[void](New-Houdini $sfx 'Houdini 21.0.440' '21.0.440')
[void](New-Houdini $sfx 'Houdini 22.0.999' '')
[void](New-Houdini $sfx 'Launcher' '')
$env:CYCLES_EASY_HOUDINI_SEARCH = $sfx
$installs = @(Get-EasyHoudiniInstalls)
Check (($installs | ForEach-Object { $_.Version }) -join ',' -eq '22.0.508,22.0.466,22.0.429') ("found " + (($installs | ForEach-Object { $_.Version }) -join ','))
Check (($installs | ForEach-Object { $_.Kit }) -join ',' -eq 'True,True,False') 'development kit flags'
Check ($installs[1].Tested -and -not $installs[0].Tested) 'tested flag'
# A kit header decides the version, not the folder name.
$odd = New-Houdini (Join-Path $Work 'other') 'Houdini 22.0.510' '22.0.511'
$env:CYCLES_EASY_HOUDINI_SEARCH = (Join-Path $Work 'other')
Check ((@(Get-EasyHoudiniInstalls)[0].Version) -eq '22.0.511') 'version from the header'
$env:CYCLES_EASY_HOUDINI_SEARCH = $sfx

# --- Target of a step: newest with kit; the setting; the per-build variables ------------------------
Check ((Get-EasyHoudiniTarget).Version -eq '22.0.508') 'default target is the newest with the kit'
$env:CYCLES_EASY_HOUDINI = '22.0.466'
Check ((Get-EasyHoudiniTarget).Root -eq $h466) 'CYCLES_EASY_HOUDINI picks one'
$env:CYCLES_EASY_HOUDINI = '22.0.429'
Check ((Get-EasyHoudiniTarget).Version -eq '22.0.508') 'a build without the kit is never a target'
$env:CYCLES_EASY_HOUDINI = $null
Set-EasyHoudiniTarget $installs[1]
Check ((Get-EasyHoudiniVersion) -eq '22.0.466' -and (Get-EasyHoudiniRoot) -eq $h466) 'per-build target variables'
Set-EasyHoudiniTarget $null
$env:CYCLES_EASY_HOUDINI_SEARCH = (Join-Path $Work 'empty')
Check ((Get-EasyHoudiniTarget).Version -eq '22.0.466' -and -not (Get-EasyHoudiniTarget).Kit) 'no Houdini: the tested build, marked without kit'
$env:CYCLES_EASY_HOUDINI_SEARCH = $sfx

# --- Package eligibility grammar (scripts/release-package.py) ----------------------------------------
Check ((ConvertFrom-EasyEligibility "houdini_version == '22.0.466'") -join ',' -eq '22.0.466') 'exact'
Check ((ConvertFrom-EasyEligibility "houdini_version == '22.0.466' or (houdini_version == '22.0.467' and `$CYCLES_HOUDINI_STRICT_BUILD != '1')") -join ',' -eq '22.0.466,22.0.467') 'alternate'
Check ((ConvertFrom-EasyEligibility "houdini_version == '22.0'") -join ',' -eq '22.0.*') 'major.minor'
Check ((ConvertFrom-EasyEligibility "houdini_version >= '22.0'") -join ',' -eq '*') 'unknown grammar counts for every build'
Check (@(ConvertFrom-EasyEligibility $false).Count -eq 0) 'disabled'
Check (Test-EasyEligible @('22.0.*') '22.0.508') 'wildcard eligible'
Check (-not (Test-EasyEligible @('22.0.466') '22.0.508')) 'exact not eligible for another'

# --- States: built, older, untested, setup, nokit, none; selection ----------------------------------
$src = Join-Path $Work 'My Builds\Cycles src'
New-Item -ItemType Directory -Path $src -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $Repo 'CMakePresets.json') -Destination $src
. (Join-Path $Repo 'scripts\build-layout.ps1')
function New-Build([string]$Version, [string]$Result, [string]$SourceId) {
    $build = Get-EasyBuildDirectoryFor -SourceRoot $src -Version $Version
    New-Item -ItemType Directory -Path (Join-Path $build 'quick-tests\t1') -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $build 'build-manifest.json') -Value ('{"v":"' + $Version + '"}') -Encoding ascii
    $hash = (Get-FileHash -LiteralPath (Join-Path $build 'build-manifest.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath (Join-Path $build 'quick-tests\t1\summary.json') -Value ('{"build_manifest_sha256":"' + $hash + '","result":"' + $Result + '"}') -Encoding ascii
    Set-Content -LiteralPath (Join-Path $build 'easy-build.json') -Value ('{"source_id":"' + $SourceId + '"}') -Encoding ascii
    return $build
}
$b466 = New-Build '22.0.466' 'passed' 'git:old'
Check ($b466 -like '*Cycles src\_work\windows-h22.0.466-release') "one build folder per Houdini build, inside the project folder: $b466"
$prefs = Join-Path $Work 'prefs\houdini__HVER__'
$env:CYCLES_EASY_HOUDINI_PREFS = $prefs
Check ((Get-EasyPreferencesFolder) -eq (Join-Path $Work 'prefs\houdini22.0')) 'preferences folder'
$rows = @(Get-EasyHoudiniStates -SourceRoot $src -SourceId 'git:new')
Check (($rows | ForEach-Object { $_.State }) -join ',' -eq 'none,older,nokit') ("states " + (($rows | ForEach-Object { $_.State }) -join ','))
$rows = @(Get-EasyHoudiniStates -SourceRoot $src -SourceId 'git:old')
Check ($rows[1].State -eq 'built') 'current build'
Check ((Format-EasyHoudiniState $rows[0]) -eq 'Houdini 22.0.508: no Cycles yet; not the tested build') (Format-EasyHoudiniState $rows[0])
Check ((Format-EasyHoudiniState $rows[2]) -like 'Houdini 22.0.429: no development kit*') (Format-EasyHoudiniState $rows[2])
Check (((Select-EasyHoudiniTargets $rows 'missing').Targets | ForEach-Object { $_.Version }) -join ',' -eq '22.0.508') 'missing = without Cycles'
Check (((Select-EasyHoudiniTargets $rows '').Targets | ForEach-Object { $_.Version }) -join ',' -eq '22.0.508') 'default is missing'
Check (((Select-EasyHoudiniTargets $rows 'all').Targets | ForEach-Object { $_.Version }) -join ',' -eq '22.0.508,22.0.466') 'all = every one with the kit'
Check (((Select-EasyHoudiniTargets $rows 'newest').Targets | ForEach-Object { $_.Version }) -join ',' -eq '22.0.508') 'newest'
Check ((Select-EasyHoudiniTargets $rows '22.0.429').Problem -like '*without its development kit*') 'no kit: plain sentence'
Check ((Select-EasyHoudiniTargets $rows '22.0.111').Problem -like '*not installed*') 'not installed: plain sentence'
Check ((Select-EasyHoudiniTargets $rows 'everything').Problem -like 'Unknown*') 'unknown choice'
# A failed quick test does not count as having Cycles.
[void](New-Build '22.0.508' 'failed' 'git:old')
$rows = @(Get-EasyHoudiniStates -SourceRoot $src -SourceId 'git:old')
Check ($rows[0].State -eq 'untested') 'untested'
Check (((Select-EasyHoudiniTargets $rows 'missing').Targets | ForEach-Object { $_.Version }) -join ',' -eq '22.0.508') 'untested counts as missing'
Remove-Item -LiteralPath (Get-EasyBuildDirectoryFor -SourceRoot $src -Version '22.0.508') -Recurse -Force
# A Setup install (official or community) for 22.0.508: has Cycles, left alone by "missing".
New-Item -ItemType Directory -Path (Join-Path $Work 'prefs\houdini22.0\packages') -Force | Out-Null
Set-Content -LiteralPath (Join-Path $Work 'prefs\houdini22.0\packages\cycles_houdini_22_0_508.json') -Value '{"enable":"houdini_version == ''22.0.508''","env":[{"CYCLES_HOUDINI_RELEASE_ID":"x"}]}' -Encoding ascii
$rows = @(Get-EasyHoudiniStates -SourceRoot $src -SourceId 'git:old')
Check ($rows[0].State -eq 'setup' -and $rows[0].Other.Count -eq 1) 'Setup install found'
Check ((Format-EasyHoudiniState $rows[0]) -like '*installed for it (cycles_houdini_22_0_508.json)*') (Format-EasyHoudiniState $rows[0])
Check (@((Select-EasyHoudiniTargets $rows 'missing').Targets).Count -eq 0) 'missing skips a Setup install'
Check ((Select-EasyHoudiniTargets $rows '22.0.508').Targets.Count -eq 1) 'an explicit choice still builds (own folder)'

# --- The installers: names, folder, the plain lines of the first-run list ----------------------------
Check ((Get-EasySetupFileName 'Cycles for Houdini 22.0.508 (community)') -eq 'Cycles for Houdini 22.0.508 (community) - Setup.exe') 'Setup file name'
Check ((Get-EasyInstallerFolder) -eq (Join-Path $script:EasyRoot 'Installers')) 'installer folder inside the project folder'
Check ((Get-EasyBuildDirectory) -like ((Join-Path $script:EasyRoot '_work') + '\windows-h*-release')) ('build folder inside the project folder: ' + (Get-EasyBuildDirectory))
$env:CYCLES_CUDA_ROOT = $null
Check ((Get-EasyCudaPartsFolder) -eq (Join-Path $script:EasyRoot ('_work\cuda-' + (Get-EasyCudaPartsVersion)))) ('CUDA compiler parts inside the project folder: ' + (Get-EasyCudaPartsFolder))
Check ((Get-EasyCudaPartsVersion) -match '^\d+\.\d+\.\d+$') 'CUDA parts version read from cuda-toolkit-parts.py'
$earlier = @(Get-EasyEarlierFolders)
Check ($earlier.Count -eq 2 -and $earlier[0] -eq ($script:EasyRoot + '-build') -and $earlier[1] -eq ($script:EasyRoot + '-installers')) ('earlier layout folders: ' + ($earlier -join ', '))
$env:CYCLES_EASY_EARLIER_TOOLS = 'X:\earlier'
Check ((Get-EasyEarlierToolsFolder) -eq 'X:\earlier') 'earlier tools folder setting'
$env:CYCLES_EASY_EARLIER_TOOLS = $null
Check ((Get-EasyEarlierToolsFolder) -like '*\CyclesForHoudiniTools') 'earlier tools folder default'
$parts = @(Get-EasyNvidiaInstallerParts $true $true $true)
Check ($parts.Count -eq 3 -and $parts[0] -like '*CUDA compiler*' -and $parts[1] -like '*OptiX*' -and $parts[2] -like 'DLSS*') ('NVIDIA parts: ' + ($parts -join ' | '))
Check (@(Get-EasyNvidiaInstallerParts $true $true $false).Count -eq 2) 'DLSS off: no DLSS part'
Check (@(Get-EasyNvidiaInstallerParts $false $true $true).Count -eq 0) 'no CUDA compiler: CPU-only, no NVIDIA part'
Check (@(Get-EasyNvidiaInstallerParts $true $false $true).Count -eq 0) 'no NVIDIA card: no NVIDIA part'
$lines = @(Get-EasyInstallerLines $parts)
Check ($lines.Count -eq 2 -and $lines[0] -like 'The result is an installer*' -and $lines[1] -like '*licences apply to you (docs\dev\nvidia-licences.md)*') ('installer lines: ' + ($lines -join ' | '))
Check (@(Get-EasyInstallerLines @()).Count -eq 1) 'CPU-only: one line'
$sourcesGB = if (Test-Path -LiteralPath (Join-Path $script:EasyRoot 'third_party\cycles')) { 0 } else { 1.3 }
Check ((Get-EasyDiskNeedGB 3) -eq [math]::Round($sourcesGB + 3 * 1.7 + 1, 1)) ('disk need for three new Houdini builds: ' + (Get-EasyDiskNeedGB 3))
Check ((Get-EasyDiskNeedGB 0 2) -eq [math]::Round($sourcesGB + 2 * 0.3 + 1, 1)) 'disk need for two rebuilds'
Check ($null -ne (Get-EasyFreeGB $script:EasyRoot)) 'free space of this drive can be read'
Check ((Get-EasyDiskProblem 100000) -like 'Not enough free disk space: drive *') 'a plain sentence when there is too little'
Check ($null -eq (Get-EasyPathProblem 'C:\Cycles')) 'a short project path is fine'
Check ((Get-EasyPathProblem ('C:\' + ('x' * 90))) -like '*Move the folder closer to the drive root*') 'a long project path: plain sentence'

# --- GPU kernel reuse: on by default, one cache folder next to the build folders, one way to turn it off ------
$env:CYCLES_EASY_NO_KERNEL_REUSE = $null
$kc = @(Get-EasyKernelCacheArguments)
Check ($kc.Count -eq 2 -and $kc[0] -eq '-KernelCache' -and $kc[1] -eq (Join-Path (Split-Path -Parent (Get-EasyBuildDirectory)) 'kernel-cache')) ('kernel cache arguments: ' + ($kc -join ' '))
$env:CYCLES_EASY_NO_KERNEL_REUSE = '1'
Check ((@(Get-EasyKernelCacheArguments) -join ' ') -eq '-KernelCache off') 'CYCLES_EASY_NO_KERNEL_REUSE=1 turns reuse off'
$env:CYCLES_EASY_NO_KERNEL_REUSE = $null

# --- Houdini changed: 22.0.466 removed, 22.0.510 added ------------------------------------------------
$seen = @('22.0.508', '22.0.466', '22.0.429')
Remove-Item -LiteralPath $h466 -Recurse -Force
[void](New-Houdini $sfx 'Houdini 22.0.510' '22.0.510')
$rows = @(Get-EasyHoudiniStates -SourceRoot $src -SourceId 'git:old')
$changes = Get-EasyHoudiniChanges $rows $seen $src
Check ((@($changes.Gone) | ForEach-Object { $_.Version }) -join ',' -eq '22.0.466') 'the removed Houdini build is reported'
Check ((@($changes.New) | ForEach-Object { $_.Version }) -join ',' -eq '22.0.510') 'the new Houdini build is built for'
Check (@((Get-EasyHoudiniChanges $rows @() $src).New).Count -eq 0) 'first run: nothing is "new"'
$known = $seen + '22.0.510'
Check (@((Get-EasyHoudiniChanges $rows $known $src).New).Count -eq 0) 'unselected build is not retried automatically'
$retry = Get-EasyHoudiniChanges $rows $known $src @('22.0.510')
Check ((@($retry.New) | ForEach-Object { $_.Version }) -join ',' -eq '22.0.510') 'a known build without a manifest remains retryable'
# --- A running Houdini that has the build loaded stops a rebuild (injected process lists) -------------------
$h508row = [pscustomobject]@{ Version = '22.0.508'; Root = $h508 }
$b508 = Get-EasyBuildDirectoryFor -SourceRoot $src -Version '22.0.508'
$using = @(Get-EasyProcessesUsingBuild $h508row $b508 @(
    [pscustomobject]@{ Name = 'houdinifx'; Path = (Join-Path $h508 'bin\houdinifx.exe'); Modules = @((Join-Path $b508 'hosts\20261121T1\dso\_cycles_houdini.dll'), 'C:\Windows\System32\kernel32.dll') },
    [pscustomobject]@{ Name = 'hython'; Path = (Join-Path $h508 'bin\hython.exe'); Modules = @('C:\Windows\System32\kernel32.dll') }))
Check (($using -join ',') -eq 'houdinifx') ('Houdini with the build loaded: ' + ($using -join ','))
$using = @(Get-EasyProcessesUsingBuild $h508row $b508 @(
    [pscustomobject]@{ Name = 'ch_worker'; Path = (Join-Path $b508 'hosts\20261121T1\bin\ch_worker.exe'); Modules = @() }))
Check (($using -join ',') -eq 'ch_worker') 'its render worker'
$using = @(Get-EasyProcessesUsingBuild $h508row $b508 @(
    [pscustomobject]@{ Name = 'hython'; Path = 'C:\Program Files\Side Effects Software\Houdini 22.0.466\bin\hython.exe'; Modules = @((Join-Path $Work 'other-build\x.dll')) },
    [pscustomobject]@{ Name = 'houdini'; Path = (Join-Path $h508 'bin\houdini.exe'); Modules = @('E:\elsewhere\_cycles_houdini.dll') }))
Check ($using.Count -eq 0) ('another Houdini, or one without this build: ' + ($using -join ','))
Check ((Get-EasyCloseHoudiniSentence '22.0.508') -eq 'Close Houdini 22.0.508 first (it is using your build), then run this again.') 'the sentence'

# A detectable compiler cannot authorize an NVIDIA download.
$env:CYCLES_CUDA_ROOT = Join-Path $Work 'cuda'
$bin = Join-Path $env:CYCLES_CUDA_ROOT 'bin'
New-Item -ItemType Directory -Path $bin -Force | Out-Null
Set-Content -LiteralPath (Join-Path $bin 'nvcc.exe') -Value 'fixture marker, never executed'
$env:CYCLES_EASY_NO_NVIDIA = '1'
$env:CYCLES_EASY_NVIDIA_CONSENT = 'yes'
$identity = [pscustomobject]@{ dlss = 'on' }
$plan = Get-EasyNvidiaPlan $identity
Check (-not $plan.Nvidia -and $plan.DlssWanted -and -not $plan.Dlss -and -not $plan.Optix) 'no card even with a compiler and approval: CPU-only'
Check (@(Get-EasyCudaArguments $plan).Count -eq 0) 'no compiler argument'
function Get-EasyNvidiaDriver { [pscustomobject]@{ Gpu = 'Fixture NVIDIA'; Version = [version]'590.0' } }
$env:CYCLES_EASY_NVIDIA_CONSENT = 'no'
$plan = Get-EasyNvidiaPlan $identity
Check ($plan.Nvidia -and -not $plan.Allowed -and -not $plan.Optix -and -not $plan.Dlss) 'explicit no: CPU-only'
$env:CYCLES_EASY_NVIDIA_CONSENT = ''
$env:CYCLES_EASY_ANSWER = 'yes'
$script:EasyWork = Join-Path $Work '_work'
$plan = Get-EasyNvidiaPlan $identity -Ask
Check (-not $plan.Allowed) 'ordinary yes does not authorize NVIDIA'
$env:CYCLES_EASY_NVIDIA_CONSENT = 'yes'
$plan = Get-EasyNvidiaPlan $identity
Check ($plan.Allowed -and $plan.Optix -and $plan.Dlss -and @(Get-EasyCudaArguments $plan).Count -eq 2) 'separate yes: GPU build'
$plan = Get-EasyNvidiaPlan ([pscustomobject]@{ dlss = 'off' })
Check ($plan.Optix -and -not $plan.DlssWanted -and -not $plan.Dlss) 'DLSS off leaves OptiX'
Check ((Get-EasyBuildArguments ([pscustomobject]@{ dlss = 'on'; dlss_project_id = 'a0f57b54-1daf-4934-90ae-c4035c19df04' }) -NoDlss) -join ' ' -eq '-Dlss:$false -DlssProjectId none') 'no DLSS build switches'
Check ((Get-EasyBuildArguments ([pscustomobject]@{ dlss = 'off' })) -join ' ' -eq '-Dlss:$false -DlssProjectId none') 'off build switches'

if ($failures.Count) { $failures | ForEach-Object { Write-Output "FAIL: $_" }; exit 1 }
Write-Output "PASSED in PowerShell $($PSVersionTable.PSVersion)"
exit 0
"""


def powershells():
    found = []
    pwsh = shutil.which("pwsh") or (
        str(Path(os.environ.get("ProgramFiles", "")) / "PowerShell/7/pwsh.exe")
        if (Path(os.environ.get("ProgramFiles", "")) / "PowerShell/7/pwsh.exe").is_file()
        else None
    )
    if pwsh:
        found.append(pwsh)
    windows = Path(os.environ.get("SystemRoot", "")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if os.name == "nt" and windows.is_file():
        found.append(str(windows))
    return found


class EasyHoudiniTest(unittest.TestCase):
    """Houdini targeting, build and retry state, installer labels and NVIDIA choices with fixtures."""

    def test_detection_states_selection_and_installer_names(self):
        shells = powershells()
        if not shells:
            self.skipTest("no PowerShell on this computer")
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "easy_houdini_test.ps1"
            script.write_text(EASY_HOUDINI_TEST, encoding="ascii")
            for index, shell in enumerate(shells):
                work = Path(temporary) / f"work {index}"
                work.mkdir()
                # PSModulePath of a PowerShell 7 parent would make Windows PowerShell 5.1 load PowerShell 7's
                # modules (Get-FileHash then is missing); each shell uses its own default here.
                env = {
                    k: v
                    for k, v in os.environ.items()
                    if not k.upper().startswith(("CYCLES_EASY", "CYCLES_CUDA", "HOUDINI", "PSMODULEPATH"))
                }
                done = subprocess.run(
                    [
                        shell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(script),
                        "-Repo",
                        str(ROOT),
                        "-Work",
                        str(work),
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=600,
                )
                self.assertEqual(done.returncode, 0, shell + "\n" + done.stdout[-3000:] + done.stderr[-3000:])
                self.assertIn("PASSED", done.stdout)

    def test_auto_reports_failed_new_target_and_update_retries_it(self):
        shells = [shell for shell in powershells() if Path(shell).stem.casefold() == "pwsh"]
        if not shells:
            self.skipTest("PowerShell 7 is required for the builder")
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            scripts = work / "scripts"
            scripts.mkdir()
            for name in ("easy-start.ps1", "easy-common.ps1", "easy-houdini.ps1", "build-layout.ps1"):
                shutil.copyfile(ROOT / "scripts" / name, scripts / name)
            for name in ("source.lock.json", "CMakePresets.json"):
                shutil.copyfile(ROOT / name, work / name)
            houdini = work / "hosts"
            for version in ("22.0.508", "22.0.466"):
                header = houdini / ("Houdini " + version) / "toolkit/include/SYS/SYS_Version.h"
                header.parent.mkdir(parents=True)
                header.write_text(f'#define SYS_VERSION_FULL "{version}"\n', encoding="ascii")
            prefs = work / "preferences/packages"
            prefs.mkdir(parents=True)
            registration = prefs / "cycles_houdini_mine.json"
            original = b'{"enable": false, "env": [{"FIXTURE": "unchanged"}]}'
            registration.write_bytes(original)
            state = work / "_work/easy-houdini.json"
            state.parent.mkdir()
            state.write_text(json.dumps({"seen": ["22.0.508"]}), encoding="utf-8")
            existing = state.parent / "windows-h22.0.508-release"
            (existing / "quick-tests").mkdir(parents=True)
            manifest = b"{}"
            (existing / "build-manifest.json").write_bytes(manifest)
            (existing / "quick-tests/summary.json").write_text(
                json.dumps({"result": "passed", "build_manifest_sha256": hashlib.sha256(manifest).hexdigest()}),
                encoding="utf-8",
            )

            (scripts / "easy-get-and-build.ps1").write_text(
                """param([int]$Jobs, [switch]$NoUpdate)
if ($env:CYCLES_EASY_TARGET_HOUDINI -ne '22.0.466') { exit 91 }
$marker = Join-Path $PSScriptRoot 'attempted'
if (-not (Test-Path -LiteralPath $marker)) {
    Set-Content -LiteralPath $marker -Value 'first attempt'
    exit 1
}
exit 0
""",
                encoding="ascii",
            )
            (scripts / "easy-quick-tests.ps1").write_text("exit 0\n", encoding="ascii")
            (scripts / "easy-make-installer.ps1").write_text(
                """param([int]$Jobs, [string]$ResultFile, [switch]$StopBeforeInstaller, [switch]$Tested)
if (-not $StopBeforeInstaller) { exit 91 }
Set-Content -LiteralPath $ResultFile -Value '{"package": "fixture"}'
exit 0
""",
                encoding="ascii",
            )
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith(("CYCLES_EASY", "CYCLES_CUDA", "HOUDINI", "PSMODULEPATH"))
            }
            env.update(
                CYCLES_EASY_NO_NVIDIA="1",
                CYCLES_EASY_HOUDINI_SEARCH=str(houdini),
                CYCLES_EASY_HOUDINI_PREFS=str(prefs.parent),
            )
            command = [
                shells[0],
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "easy-start.ps1"),
                "-StopBeforeInstaller",
                "-Yes",
                "-NoUpdate",
            ]
            first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
            self.assertIn("NOT DONE: No build finished", first.stdout)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["retry"], ["22.0.466"])
            next_run = subprocess.run(
                [*command, "-Action", "Update", "-Houdini", "22.0.466"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(next_run.returncode, 0, next_run.stdout + next_run.stderr)
            self.assertIn("packages were verified", next_run.stdout)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["retry"], [])
            self.assertEqual(registration.read_bytes(), original)

    def test_fetch_failure_stops_before_dependencies_and_no_update_works(self):
        shells = [shell for shell in powershells() if Path(shell).stem.casefold() == "pwsh"]
        if not shells or not shutil.which("git"):
            self.skipTest("PowerShell 7 and Git are required for the builder")
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            scripts = work / "scripts"
            scripts.mkdir()
            for name in (
                "easy-get-and-build.ps1",
                "easy-common.ps1",
                "easy-houdini.ps1",
                "build-layout.ps1",
                "release-package.py",
            ):
                shutil.copyfile(ROOT / "scripts" / name, scripts / name)
            for name in ("source.lock.json", "CMakePresets.json", "build-identity.json"):
                shutil.copyfile(ROOT / name, work / name)
            (scripts / "bootstrap.ps1").write_text(
                """$args | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'bootstrap-args.txt')
exit 0
""",
                encoding="ascii",
            )
            (scripts / "build.ps1").write_text(
                """$root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Path (Join-Path $root '_work/windows-h22.0.466-release') -Force | Out-Null
exit 0
""",
                encoding="ascii",
            )

            def run_git(*args):
                return subprocess.check_output(
                    [
                        "git",
                        "-c",
                        "user.name=Fixture Builder",
                        "-c",
                        "user.email=" + "@".join(("fixture", "localhost.invalid")),
                        "-c",
                        "core.hooksPath=NUL",
                        "-c",
                        "commit.gpgSign=false",
                        "-C",
                        str(work),
                        *args,
                    ],
                    text=True,
                    stderr=subprocess.PIPE,
                ).strip()

            run_git("init", "--initial-branch=fixture")
            run_git("add", "--", ".")
            run_git("commit", "-m", "Fixture source")
            run_git("remote", "add", "origin", str(work / "offline"))
            run_git("update-ref", "refs/remotes/origin/fixture", run_git("rev-parse", "HEAD"))
            run_git("branch", "--set-upstream-to=origin/fixture")
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.upper().startswith(("CYCLES_EASY", "CYCLES_CUDA", "HOUDINI", "PSMODULEPATH"))
            }
            env.update(
                CYCLES_EASY_NO_NVIDIA="1",
                CYCLES_EASY_TARGET_HOUDINI="22.0.466",
                CYCLES_EASY_TARGET_ROOT=str(work / "unused-host"),
                CYCLES_EASY_HOUDINI_PREFS=str(work / "unused-prefs"),
                GIT_TERMINAL_PROMPT="0",
            )
            command = [
                shells[0],
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "easy-get-and-build.ps1"),
            ]
            failed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(failed.returncode, 6, failed.stdout + failed.stderr)
            self.assertIn("Git could not fetch", failed.stdout)
            dependency_args = scripts / "bootstrap-args.txt"
            self.assertFalse(dependency_args.exists())
            continued = subprocess.run([*command, "-NoUpdate"], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(continued.returncode, 0, continued.stdout + continued.stderr)
            options = dependency_args.read_text(encoding="utf-8-sig").splitlines()
            self.assertTrue({"-NoCuda", "-SkipOptix", "-SkipDlss"}.issubset(options), options)

    def test_installer_source_provenance_ignores_parent_git_and_accepts_linked_worktree(self):
        shells = [shell for shell in powershells() if Path(shell).stem.casefold() == "pwsh"]
        if not shells or not shutil.which("git"):
            self.skipTest("PowerShell 7 and Git are required for the builder")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            archive = checkout / "unpacked-archive"
            worktree = root / "linked-worktree"
            inputs = (
                "build-identity.json",
                "source.lock.json",
                "packaging/installer/CyclesHoudini.iss",
                "patches/0077-houdini-dlss-project.patch",
                "scripts/easy-common.ps1",
                "scripts/easy-houdini.ps1",
                "scripts/easy-make-installer.ps1",
                "scripts/release-package.py",
            )

            def populate(destination):
                for name in inputs:
                    target = destination / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(ROOT / name, target)

            populate(checkout)
            git(checkout, "init", "--quiet", "--template=")
            (checkout / ".gitignore").write_text("unpacked-archive/\n", encoding="ascii")
            git(checkout, "add", ".")
            git(
                checkout,
                "-c",
                "user.name=Fixture Publisher",
                "-c",
                "user.email=" + "@".join(("fixture", "localhost.invalid")),
                "-c",
                "core.hooksPath=NUL",
                "-c",
                "commit.gpgSign=false",
                "commit",
                "--quiet",
                "-m",
                "Fixture source",
            )
            revision = git(checkout, "rev-parse", "HEAD")
            git(checkout, "-c", "core.hooksPath=NUL", "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")
            populate(archive)
            self.assertEqual(git(archive, "rev-parse", "HEAD"), revision, "the parent must be visible to raw Git")
            self.assertFalse((archive / ".git").exists())
            self.assertTrue((worktree / ".git").is_file())

            env = {
                name: value
                for name, value in os.environ.items()
                if not name.upper().startswith(("CYCLES_EASY", "CYCLES_CUDA", "HOUDINI", "PSMODULEPATH"))
            }
            # Stop at the missing host after source provenance is printed, before build or packaging.
            env.update(
                CYCLES_EASY_TARGET_HOUDINI="22.0.466",
                CYCLES_EASY_TARGET_ROOT=str(root / "nonexistent-host"),
                CYCLES_EASY_HOUDINI_PREFS=str(root / "unused-prefs"),
            )
            for folder in (archive, checkout, worktree):
                done = subprocess.run(
                    [
                        shells[0],
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(folder / "scripts/easy-make-installer.ps1"),
                        "-StopBeforeInstaller",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
                self.assertIn("Houdini's Python is missing", done.stdout)
                if folder == archive:
                    self.assertIn('records its source commit and local changes as "unknown"', done.stdout)
                    self.assertNotIn(revision, done.stdout)
                else:
                    self.assertIn(f"Commit {revision}", done.stdout)
                    self.assertIn("No uncommitted changes", done.stdout)

    def test_the_installer_path_never_runs_a_setup(self):
        # The packaging step does not start its generated Setup.
        maker = (ROOT / "scripts/easy-make-installer.ps1").read_text(encoding="utf-8")
        for never in ("Read-EasyYesNo", "Read-Host"):
            self.assertNotIn(never, maker)
        start = (ROOT / "scripts/easy-start.ps1").read_text(encoding="utf-8")
        for gone in (
            "Invoke-EveryStart",
            "Invoke-OfferHoudini",
            "with your build now?",
            "'EveryStart'",
            "'StopEveryStart'",
        ):
            self.assertNotIn(gone, start)
        for script in sorted((ROOT / "scripts").glob("easy-*.ps1")):
            text = script.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(Start-Process|&)[^\n]*Setup\.exe", text), script.name)
        # The menu: Enter = Close.
        self.assertIn("if (-not $answer) { $answer = '0' }", start)

    def test_kernel_reuse_only_on_the_builders_path(self):
        # The official route never reuses GPU kernels: it passes no -KernelCache, and build.ps1 adds the
        # engine's CUDA<major>_NVCC_EXECUTABLE setting only inside the -KernelCache branch.
        for script in ("build-release.ps1", "build-installer.ps1"):
            self.assertNotIn("KernelCache", (ROOT / "scripts" / script).read_text(encoding="utf-8"), script)
        build = ps_code((ROOT / "scripts/build.ps1").read_text(encoding="utf-8")).split("\n")
        self.assertIn("    [string]$KernelCache = '',", build)
        start = build.index("if($KernelCache -and $KernelCache -ne 'off' -and -not $NoCuda){")
        end = build.index("}", start)
        defined = [i for i, line in enumerate(build) if "-DCUDA${major}_" in line]
        self.assertTrue(defined and all(start < i < end for i in defined), defined)
        self.assertIn(
            "(Get-EasyKernelCacheArguments)", (ROOT / "scripts/easy-get-and-build.ps1").read_text(encoding="utf-8")
        )
        self.assertIn(
            "Invoke-EasyScript 'easy-get-and-build.ps1'",
            (ROOT / "scripts/easy-make-installer.ps1").read_text(encoding="utf-8"),
        )

    def test_community_builds_may_target_any_22_0_build_the_official_one_only_the_qualified(self):
        mine = PACKAGE["target_identity"]("22.0.508", community())
        self.assertEqual(mine["package_filename"], "cycles_houdini_community_community_22_0_508.json")
        self.assertEqual(mine["default_directory"], "Cycles for Houdini 22.0.508 (community)")
        for host in ("22.0.508", "22.0.999"):
            with self.assertRaisesRegex(PACKAGE["PackageError"], "Unqualified"):
                PACKAGE["target_identity"](host)
        for host in ("21.0.440", "22.1.10", "22.0", "22.0.508.1"):
            with self.assertRaisesRegex(PACKAGE["PackageError"], "Unqualified"):
                PACKAGE["target_identity"](host, community())

    def test_no_dlss_and_cpu_only_switches_leave_the_default_route_as_it_was(self):
        bootstrap = (ROOT / "scripts/bootstrap.ps1").read_text(encoding="utf-8")
        skip = bootstrap.index("if ($SkipDlss) {")
        self.assertLess(skip, bootstrap.index("Sync-PinnedHeaders $dlss $lock.dlss_headers"))
        self.assertIn("if (-not $NoCuda -and -not (Test-Path -LiteralPath (Join-Path $CudaRoot", bootstrap)
        build = (ROOT / "scripts/build.ps1").read_text(encoding="utf-8")
        self.assertIn(
            "if($NoCuda){$argsConfigure += @('-DWITH_CYCLES_DEVICE_CUDA=OFF','-DWITH_CYCLES_DEVICE_OPTIX=OFF',"
            "'-DWITH_CYCLES_CUDA_BINARIES=OFF')}",
            build,
        )
        self.assertNotIn("NoCuda", (ROOT / "scripts/build-release.ps1").read_text(encoding="utf-8"))
        stage = (ROOT / "scripts/stage-development.ps1").read_text(encoding="utf-8")
        self.assertIn("-notin @('ON','TRUE','1')){Write-Host \"No $backend kernels", stage)
        # DLSS stays on by default for community builds (owner's decision, 21 Nov 2026).
        self.assertEqual(json.loads((ROOT / "build-identity.json").read_text(encoding="utf-8"))["dlss"], "on")

    def test_the_one_driver_statement_and_no_driver_install(self):
        common = (ROOT / "scripts/easy-common.ps1").read_text(encoding="utf-8")
        statement = (
            "GPU rendering and GPU denoising need an NVIDIA graphics card with its driver installed. "
            "Installing or updating the driver is up to you."
        )
        self.assertIn("$script:EasyDriverStatement = '" + statement + "'", common)
        guide = (ROOT / "docs/user/build-it-yourself.md").read_text(encoding="utf-8")
        self.assertIn(statement, " ".join(guide.split()))
        for name in sorted((ROOT / "scripts").glob("easy-*")) + [ROOT / "scripts/cuda-toolkit-parts.py"]:
            text = name.read_text(encoding="utf-8").lower()
            for never in ("display.driver", "nvidia.geforceexperience", "nvidia.app", "winget install --id nvidia"):
                self.assertNotIn(never, text, name.name)


class FreshCloneJudgeTest(unittest.TestCase):
    """scripts/fresh-clone-judge.py judged against the recorded states of earlier runs (fixtures in
    tests/fixtures/fresh_clone_judge/, trimmed from the runs' result files): each run is judged as it should have been,
    including the runs whose own drivers judged wrongly."""

    FIXTURES = ROOT / "tests/fixtures/fresh_clone_judge"

    def setUp(self):
        self.j = runpy.run_path(str(ROOT / "scripts/fresh-clone-judge.py"))

    def run_record(self, name):
        return json.loads((self.FIXTURES / (name + ".json")).read_text(encoding="utf-8"))

    def assertJudged(self, result, ok, reason=None):
        self.assertEqual(result["ok"], ok, result["reason"])
        if reason:
            self.assertIn(reason, result["reason"])

    def test_dry_driver_uses_build_order_and_fails_red_or_unsupported_replay(self):
        shells = powershells()
        if not shells:
            self.skipTest("no PowerShell on this computer")
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            driver = ROOT / "scripts/fresh-clone-community-check.ps1"
            source = work / "source"
            (source / "scripts").mkdir(parents=True)
            shutil.copyfile(ROOT / "scripts/fresh-clone-judge.py", source / "scripts/fresh-clone-judge.py")
            for index, (name, expected) in enumerate((("k-c1", 1), ("k-c2e", 1), ("passing", 0))):
                fixture = self.FIXTURES / (name + ".json")
                if name == "passing":
                    record = self.run_record("k-c1")
                    record["kernels"].pop("checks")
                    fixture = work / "passing.json"
                    fixture.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
                result = work / f"result{index}.json"
                done = subprocess.run(
                    [
                        shells[0],
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(driver),
                        "-DryJudges",
                        str(fixture),
                        "-Destination",
                        str(work / f"dry{index}"),
                        "-ResultFile",
                        str(result),
                        "-Source",
                        str(source),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(done.returncode, expected, done.stdout + done.stderr)
                self.assertTrue(result.is_file(), done.stdout + done.stderr)
                verdicts = json.loads(result.read_text(encoding="utf-8-sig"))["verdicts"]
                if name == "k-c2e":
                    self.assertIn("ordered versions", verdicts["input"])
                else:
                    self.assertEqual(verdicts["first-run"], "passed")
                    self.assertEqual(verdicts["first-run 22.0.429"], "passed")
                    if name == "k-c1":
                        self.assertTrue(verdicts["reuse-on-again"].startswith("failed:"))

    def test_k_c1_three_builds_and_the_unstable_key(self):
        k = self.run_record("k-c1")["kernels"]
        first = k["first_run"]
        self.assertJudged(self.j["first_compile"](first["22.0.508"]), True)
        for v in ("22.0.466", "22.0.429"):
            self.assertJudged(self.j["reuse_all"](first[v], first["22.0.508"]), True)
        for when in ("update_unchanged", "update_after_engine_edit_outside_the_kernels"):
            for v, state in k[when].items():
                self.assertJudged(self.j["kept"](state, first[v]), True)
        self.assertJudged(self.j["reference_compile"](k["checks"]["reuse_off"], first), True)
        # The real failure of that run: the key changed with the order of PATH (fixed in the key, schema 2).
        self.assertJudged(self.j["reuse_all"](k["checks"]["reuse_on_again"], first["22.0.508"]), False, "compiled")

    def test_k_c2b_the_run_was_right_its_driver_was_not(self):
        s = self.run_record("k-c2b")["states"]
        self.assertJudged(self.j["reference_compile"](s["reuse_off_first"], {}), True)
        # The driver failed this step on its own slash-only search of build.ninja; judged on the record it passed.
        self.assertJudged(self.j["first_compile"](s["on_new_schema"]), True)
        self.assertJudged(
            self.j["reuse_all"](
                dict(s["on_new_schema"], compiled=[], reused=list(self.j["ALL"])), s["reuse_off_first"]
            ),
            True,
        )

    def test_k_c2c_the_lost_decision_line(self):
        s = self.run_record("k-c2c")["states"]
        self.assertJudged(self.j["reuse_all"](s["other_houdini_path_doubled"], s["on_new_schema"]), True)
        self.assertJudged(self.j["reference_compile"](s["reuse_off"], {"schema 2": s["on_new_schema"]}), True)
        # The tool's real fault (fixed: one file per decision): six reused, five recorded.
        self.assertJudged(
            self.j["reuse_all"](s["reuse_on_again_path_doubled"], s["reuse_off"]), False, "missing: kernel_sm_89.cubin"
        )

    def test_k_c2d_a_new_tool_is_not_a_ninja_input(self):
        s = self.run_record("k-c2d")["states"]
        self.assertJudged(self.j["first_compile"](s["on_new_schema"]), False, "no kernel rule ran")

    def test_k_c2e(self):
        record = self.run_record("k-c2e")
        s = record["states"]
        self.assertJudged(self.j["first_compile"](s["on_new_schema"]), True)
        self.assertJudged(
            self.j["reuse_all"](
                s["on_new_schema"] | {"compiled": [], "reused": list(self.j["ALL"])}, s["reuse_off_first"]
            ),
            True,
        )
        self.assertJudged(self.j["reuse_all"](s["other_houdini_path_doubled"], s["on_new_schema"]), True)
        self.assertJudged(self.j["reference_compile"](s["reuse_off"], {"new tool": s["on_new_schema"]}), True)
        self.assertJudged(self.j["reuse_all"](s["reuse_on_again_path_doubled"], s["reuse_off"]), True)
        if "comment_edit" in s:
            self.assertJudged(self.j["new_key_compiles_all"](s["comment_edit"]), True)
        if "real_edit" in s:
            # The driver expected the OptiX kernels "reused" in the newest build; their rules do not depend on
            # device/cuda/kernel.cu, so ninja does not run them: judged correctly, the step passed.
            self.assertJudged(self.j["real_edit"](s["real_edit"], "22.0.508", s["comment_edit"]), True)
            self.assertEqual(s["real_edit"]["22.0.508"]["reused"], [])

    def test_judges_reject_what_they_must(self):
        state = {
            "mode": "on",
            "state": "reused",
            "reused": list(self.j["ALL"]),
            "compiled": [],
            "decisions": [{}],
            "files": {f"f{n}": "a" for n in range(12)},
        }
        self.assertJudged(self.j["reuse_all"](state, state), True)
        self.assertJudged(self.j["reuse_all"](dict(state, files={"f0": "a"}), state), False, "expected 12")
        self.assertJudged(
            self.j["reuse_all"](state, dict(state, files={f"f{n}": "b" for n in range(12)})), False, "differ"
        )
        self.assertJudged(self.j["reuse_all"](dict(state, reused=[], decisions=[]), state), False, "no kernel rule ran")
        self.assertJudged(self.j["reference_compile"](dict(state, mode="off"), {}), False, "decisions")
        self.assertJudged(self.j["cache_entries"]({"a": 2, "b": 3}), False, "more than two")
        self.assertJudged(self.j["cache_entries"]({}), False)
        self.assertJudged(
            self.j["damaged"](
                dict(state, damaged_ignored=True, decisions=[{"output": "x", "decision": "reused"}]), "x", state
            ),
            False,
        )

    def test_powershell_51_array_shape_is_read_as_a_list(self):
        names = list(self.j["ALL"])
        wrapped = {
            "mode": "on",
            "reused": {"value": names, "Count": 6},
            "compiled": {"value": [], "Count": 0},
            "decisions": {"value": [{"output": n} for n in names], "Count": 6},
            "files": {f"f{n}": "a" for n in range(12)},
        }
        self.assertJudged(self.j["reuse_all"](wrapped, wrapped), True)
        self.assertJudged(
            self.j["reuse_all"](dict(wrapped, reused="kernel_optix.ptx"), wrapped), False, "reused 1 of six"
        )

    def test_x_c1_empty_listings(self):
        record = self.run_record("x-c1")
        self.assertIn("ReferenceObject", record["error"])  # the check's own compare failed on an empty list
        for before, after in ((None, None), ([], []), (None, []), ([], None)):
            self.assertJudged(self.j["nothing_outside"](before, after), True)
        self.assertEqual(self.j["listing_changes"](["a|1"], ["a|1", "b|2"]), ["b|2"])
        self.assertJudged(self.j["nothing_outside"]([], ["new"]), False, "1 change")

    def test_y_path_rounds_and_the_boundary_computed_twice(self):
        recorded = self.run_record("y-c1c")["path_length"]
        self.assertJudged(self.j["path_rounds"](recorded), True)
        # y-c1b as it should have been judged: the check's own "259" folder gave 260 (now recorded and judged).
        wrong = json.loads(json.dumps(recorded))
        wrong["at_the_limit"]["root_plus_below"] = 260
        self.assertJudged(self.j["path_rounds"](wrong), False, "gives 260 characters, not 259")
        wrong = json.loads(json.dumps(recorded))
        wrong["one_over_the_limit"]["created_app_folder"] = True
        self.assertJudged(self.j["path_rounds"](wrong), False, "wrote something")
        P = runpy.run_path(str(ROOT / "scripts/release-package.py"))
        # The paths are Windows paths; the tool's own path cleaning (absolute path, no links) is for real folders.
        unlinked = patch.dict(P["install_path_problem"].__globals__, unlinked_path=lambda path: path)
        unlinked.start()
        self.addCleanup(unlinked.stop)
        names = ["provenance/patches/" + "p" * 63, "host/x.dll"]
        below = len("versions/") + 16 + 1 + len(names[0])
        base = "D:\\Example_Folder\\_work\\tests\\T08\\isolated-install\\22.0.508\\long".replace("\\\\", "\\")
        at = self.j["long_root"](base, below, 259)["root"]
        over = self.j["long_root"](base, below, 260)["root"]
        self.assertIsNone(P["install_path_problem"](at, names))
        self.assertIn("260 characters", P["install_path_problem"](over, names))
        self.assertEqual(len(over), len(at) + 1)
        # y-c1b: the check's earlier padding (no separator after the padding folder) made its "259" folder 260.
        old_pad = 259 - below - 1 - (len(base) + 1 + len("installation"))
        old_root = base + "\\" + "r" * old_pad + "\\installation"
        self.assertIn(
            f"{self.run_record('y-c1b')['at_the_limit_refused_with']} characters",
            P["install_path_problem"](old_root, names),
        )
        with self.assertRaises(ValueError):
            self.j["long_root"]("C:\\" + "x" * 300, below, 259)


# A stand-in for nvcc for the kernel cache test: --version, --dryrun (one device preprocessing step that
# runs this file in "pp" mode, like nvcc's cl.exe -E step), and a "compile" whose output is a digest of the
# preprocessed source and the options (so equal inputs give equal bytes, as nvcc does).
FAKE_NVCC = r"""
import hashlib, os, re, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
def includes(args):
    found = []
    for i, a in enumerate(args):
        if a == "-I" and i + 1 < len(args):
            found.append(args[i + 1])
        elif a.startswith("-I") and len(a) > 2:
            found.append(a[2:])
    return found
def source_of(args):
    return [a for a in args if a.endswith(".cu") and not a.startswith("-")][0]
def preprocess(path, dirs, out, seen):
    path = Path(path).resolve()
    out.append('# 1 "%s"' % str(path).replace("\\", "\\\\"))
    for number, line in enumerate(path.read_text().splitlines(), 1):
        m = re.match(r'\s*#include "([^"]+)"', line)
        if m:
            for d in [str(path.parent)] + dirs:
                candidate = Path(d) / m.group(1)
                if candidate.is_file():
                    preprocess(candidate, dirs, out, seen)
                    out.append('# %d "%s"' % (number + 1, str(path).replace("\\", "\\\\")))
                    break
            else:
                print("missing include " + m.group(1), file=sys.stderr)
                sys.exit(2)
            continue
        out.append(line.split("//")[0].rstrip())
def main(args):
    if args and args[0] == "pp":
        out = []
        preprocess(source_of(args), includes(args), out, set())
        sys.stdout.write("\n".join(out) + "\n")
        return 0
    if "--version" in args:
        print("Cuda compilation tools, release 12.8, V12.8.61")
        return 0
    if "--dryrun" in args:
        rest = [a for a in args if a != "--dryrun"]
        flags = " ".join(a for a in rest if a.startswith("-D") or a.startswith("-I") or a.startswith("-arch"))
        tmp = os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp"
        print("#$ _NVVM_BRANCH_=nvvm", file=sys.stderr)
        if not os.environ.get("FAKE_NVCC_NO_PP"):
            # As nvcc on Windows: the redirection in the middle of the line.
            print('#$ "%s" "%s" pp > "%s" -E %s %s "%s"' % (sys.executable, __file__, os.path.join(tmp, "tmpxft_%08x_00000000-6_kernel.cpp1.ii" % os.getpid()),
                  flags, " ".join('-I "%s"' % d for d in includes(rest)), source_of(rest)), file=sys.stderr)
        print("#$ FAKE_BIN=%s" % HERE, file=sys.stderr)
        print('#$ ""%%FAKE_BIN%%/cicc" -arch compute_86 "%s"' % os.path.join(tmp, "tmpxft_%08x_00000000-6_kernel.cpp1.ii" % os.getpid()), file=sys.stderr)
        return 0
    out = []
    preprocess(source_of(args), includes(args), out, set())
    text = "\n".join(out)
    if "#error" in text:
        print("error: #error in source", file=sys.stderr)
        return 1
    target = args[args.index("-o") + 1]
    options = [a for i, a in enumerate(args) if a != "-o" and (i == 0 or args[i - 1] != "-o") and not a.endswith(".cu")]
    Path(target).write_bytes(hashlib.sha256((text + "|" + " ".join(options) + "|" + (HERE / "nvcc.profile").read_text()).encode()).digest() * 64)
    with open(HERE.parent.parent / "compiles.log", "a") as log:
        log.write(target + "\n")
    return 0
sys.exit(main(sys.argv[1:]))
"""


class KernelCacheTest(unittest.TestCase):
    """scripts/kernel-cache.py with a stand-in for nvcc: reuse only when every input is the same; any change
    (even a comment), a new header found first, another compiler or setting is a new key; damaged or
    half-written entries are never used; prune keeps the newest two."""

    def setUp(self):
        self.kc = runpy.run_path(str(ROOT / "scripts/kernel-cache.py"))
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)
        cuda = self.work / "cuda parts"
        (cuda / "bin").mkdir(parents=True)
        (cuda / "include").mkdir()
        (cuda / "bin/fake_nvcc.py").write_text(FAKE_NVCC, encoding="utf-8")
        (cuda / "bin/nvcc.profile").write_text("profile 1\n", encoding="utf-8")
        (cuda / "include/cuda_runtime.h").write_text("int cuda_runtime;\n", encoding="utf-8")
        if os.name == "nt":
            nvcc = cuda / "bin/nvcc.cmd"
            nvcc.write_text(
                f'@"{sys.executable}" "%~dp0fake_nvcc.py" %*\r\n@exit /b %ERRORLEVEL%\r\n', encoding="utf-8"
            )
        else:
            nvcc = cuda / "bin/nvcc"
            nvcc.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/fake_nvcc.py" "$@"\n', encoding="utf-8"
            )
            nvcc.chmod(0o755)
        self.cuda, self.nvcc = cuda, nvcc
        src = self.work / "engine src"
        (src / "kernel/device/cuda").mkdir(parents=True)
        (src / "util").mkdir()
        (src / "kernel/device/cuda/kernel.cu").write_text(
            '#include "util/math.h"\n#include "cuda_runtime.h"\nkernel body\n', encoding="utf-8"
        )
        (src / "util/math.h").write_text("float f(); // math\n", encoding="utf-8")
        self.src = src
        self.cache = self.work / "my build-build/kernel-cache"

    def tearDown(self):
        self.temporary.cleanup()

    def compile(self, build, arch="sm_86", expect=0):
        build = self.work / "my build-build" / build
        out = build / "cycles/src/kernel/device/cuda"
        out.mkdir(parents=True, exist_ok=True)
        prepared = self.kc["prepare"](build, self.cache, self.nvcc, self.cuda)
        args = [
            f"-arch={arch}",
            "--cubin",
            str(self.src / "kernel/device/cuda/kernel.cu"),
            "-D",
            "NVCC",
            "-I",
            str(self.src),
            "-I",
            str(self.cuda / "include"),
            "-o",
            str(out / f"kernel_{arch}.cubin"),
        ]
        cwd = os.getcwd()
        os.chdir(out)
        try:
            code = self.kc["wrapper"](prepared["config"], args)
        finally:
            os.chdir(cwd)
        self.assertEqual(code, expect)
        decisions = self.kc["read_decisions"](build)
        output = out / f"kernel_{arch}.cubin"
        return (decisions[-1] if decisions else None), (output.read_bytes() if output.is_file() else None)

    def compiles(self):
        log = self.work / "compiles.log"
        return len(log.read_text().splitlines()) if log.is_file() else 0

    def entries(self):
        return (
            sorted(p for p in self.cache.iterdir() if p.is_dir() and not p.name.startswith("."))
            if self.cache.is_dir()
            else []
        )

    def test_reuse_only_for_the_same_inputs(self):
        first, made = self.compile("windows-h22.0.508-release")
        self.assertEqual(
            (first["decision"], first["why"], first["stored"], self.compiles()), ("compiled", "new", True, 1)
        )
        # Another build folder (another Houdini build), same sources: reused, byte for byte, nvcc not run.
        second, reused = self.compile("windows-h22.0.466-release")
        self.assertEqual((second["decision"], self.compiles()), ("reused", 1))
        self.assertEqual(reused, made)
        self.assertEqual(second["key"], first["key"])
        # A comment-only change in a header: the GPU program would be the same, the check still misses.
        (self.src / "util/math.h").write_text("float f(); // math, edited\n", encoding="utf-8")
        third, comment = self.compile("windows-h22.0.429-release")
        self.assertEqual((third["decision"], third["why"], self.compiles()), ("compiled", "new", 2))
        self.assertEqual(comment, made)
        # A real change: a new key and another output.
        (self.src / "util/math.h").write_text("float f(); float g();\n", encoding="utf-8")
        fourth, changed = self.compile("windows-h22.0.429-release")
        self.assertEqual((fourth["decision"], self.compiles()), ("compiled", 3))
        self.assertNotEqual(changed, made)
        # A new header of the same name found earlier in the search (next to the includer): a new key.
        (self.src / "kernel/device/cuda/util").mkdir()
        (self.src / "kernel/device/cuda/util/math.h").write_text("float f(); float g();\n", encoding="utf-8")
        fifth, _ = self.compile("windows-h22.0.429-release")
        self.assertEqual((fifth["decision"], self.compiles()), ("compiled", 4))
        self.assertNotEqual(fifth["key"], fourth["key"])

    def test_the_order_of_path_does_not_change_the_key(self):
        # Two runs with the same inputs can see PATH in another order or with an entry twice (measured on Windows
        # between the first run and a later menu run): the key stays the same and the kernel is reused.
        first, made = self.compile("windows-h22.0.508-release")
        parts = os.environ.get("PATH", "").split(os.pathsep)
        shuffled = os.pathsep.join(list(reversed(parts)) + parts[:1])
        with patch.dict(os.environ, {"PATH": shuffled}):
            second, reused = self.compile("windows-h22.0.466-release")
        self.assertEqual((second["decision"], second["key"], self.compiles()), ("reused", first["key"], 1))
        self.assertEqual(reused, made)

    def test_compiler_settings_and_architecture_are_part_of_the_key(self):
        first, _ = self.compile("a")
        (self.cuda / "bin/nvcc.profile").write_text("profile 2\n", encoding="utf-8")  # another compiler file
        second, _ = self.compile("b")
        self.assertEqual(second["decision"], "compiled")
        with patch.dict(os.environ, {"NVCC_APPEND_FLAGS": "-G"}):
            third, _ = self.compile("c")
        self.assertEqual(third["decision"], "compiled")
        fourth, _ = self.compile("d", arch="sm_89")
        self.assertEqual(fourth["decision"], "compiled")
        self.assertEqual(len({first["key"], second["key"], third["key"], fourth["key"]}), 4)
        fifth, _ = self.compile("e")  # back to the second compiler, no extra setting: reused
        self.assertEqual((fifth["decision"], fifth["key"]), ("reused", second["key"]))

    def test_damaged_or_half_written_entries_are_never_used(self):
        first, made = self.compile("a")
        (entry,) = self.entries()
        stored = entry / "kernel_sm_86.cubin"
        stored.write_bytes(b"x" + stored.read_bytes()[1:])  # same size, other bytes
        second, again = self.compile("b")
        self.assertEqual((second["decision"], second["why"], second["stored"]), ("compiled", "damaged", True))
        self.assertEqual(again, made)
        self.assertEqual(
            self.kc["sha256_file"](entry / "kernel_sm_86.cubin"),
            json.loads((entry / "manifest.json").read_text())["sha256"],
        )
        # No manifest (a copy stopped before its manifest was written): ignored.
        (entry / "manifest.json").unlink()
        third, _ = self.compile("c")
        self.assertEqual((third["decision"], third["why"]), ("compiled", "damaged"))
        # A manifest with another key: ignored.
        manifest = json.loads((entry / "manifest.json").read_text())
        manifest["key"] = "0" * 64
        (entry / "manifest.json").write_text(json.dumps(manifest))
        fourth, _ = self.compile("d")
        self.assertEqual((fourth["decision"], fourth["why"]), ("compiled", "damaged"))
        fifth, _ = self.compile("e")
        self.assertEqual(fifth["decision"], "reused")
        # A partial folder left by a stopped build is never looked at as an entry.
        (self.cache / (".partial-" + fifth["key"][:40] + "-x")).mkdir()
        self.assertEqual(self.compile("f")[0]["decision"], "reused")

    def test_failures_and_unknown_steps_compile_as_usual_and_store_nothing(self):
        (self.src / "util/math.h").write_text("#error stop\n", encoding="utf-8")
        decision, _ = self.compile("a", expect=1)
        self.assertIsNone(decision)
        self.assertEqual(self.entries(), [])
        (self.src / "util/math.h").write_text("float f();\n", encoding="utf-8")
        with patch.dict(os.environ, {"FAKE_NVCC_NO_PP": "1"}):  # --dryrun without the expected step
            decision, made = self.compile("b")
        self.assertEqual((decision["decision"], decision["why"], decision["stored"]), ("compiled", "unusable", False))
        self.assertTrue(made)
        self.assertEqual(self.entries(), [])

    def test_prepare_rechecks_existing_outputs_when_an_external_input_changes(self):
        self.compile("warm")
        build = self.work / "my build-build/warm"
        output = build / "cycles/src/kernel/device/cuda/kernel_sm_86.cubin"
        prepare = self.kc["prepare"]
        self.assertEqual(prepare(build, self.cache, self.nvcc, self.cuda, check_existing=True)["invalidated"], [])
        self.assertTrue(output.is_file())
        header = self.cuda / "include/cuda_runtime.h"
        header.write_text("int cuda_runtime; // edited in place\n", encoding="utf-8")
        self.assertEqual(
            prepare(build, self.cache, self.nvcc, self.cuda, check_existing=True)["invalidated"], [output.name]
        )
        self.assertFalse(output.exists())
        self.compile("warm")
        output.write_bytes(b"damaged warm output")
        self.assertEqual(
            prepare(build, self.cache, self.nvcc, self.cuda, check_existing=True)["invalidated"], [output.name]
        )

    def test_copy_checks_verified_digest_and_diagnostics_are_best_effort(self):
        _, first = self.compile("first")
        scope = self.kc["wrapper"].__globals__
        place = scope["place"]

        def mutate_after_check(stored, output, digest):
            stored.write_bytes(b"changed after cache validation")
            return place(stored, output, digest)

        with patch.dict(scope, place=mutate_after_check, record=lambda *_: self.fail("diagnostics unavailable")):
            decision, second = self.compile("second")
        self.assertIsNone(decision)
        self.assertEqual(second, first)
        self.assertEqual(self.compiles(), 2)

    def test_other_nvcc_calls_pass_through(self):
        from support.workers import ordinary_python

        build = self.work / "b"
        # The stand-in starts the Python that prepared it with -I. The build prepares it with ordinary Python
        # (scripts/build.ps1); this test may run inside hython, which cannot start under -I.
        with patch.object(sys, "executable", str(ordinary_python())):
            prepared = self.kc["prepare"](build, self.cache, self.nvcc, self.cuda)
        wrapper = prepared["wrapper"]
        done = subprocess.run([wrapper, "--version"], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "Cuda compilation tools, release 12.8, V12.8.61"))
        self.assertEqual(self.kc["read_decisions"](build), [])

    def test_decisions_of_kernels_built_at_the_same_time_are_all_kept(self):
        # The kernel rules run in parallel; each decision is its own file, so none can overwrite another.
        import threading

        build = self.work / "parallel"
        self.kc["prepare"](build, self.cache, self.nvcc, self.cuda)
        config = {"build": str(build)}
        threads = [
            threading.Thread(target=self.kc["record"], args=(config, {"output": f"k{n}", "decision": "reused"}))
            for n in range(24)
        ]
        [x.start() for x in threads]
        [x.join() for x in threads]
        self.assertEqual(
            sorted(d["output"] for d in self.kc["read_decisions"](build)), sorted(f"k{n}" for n in range(24))
        )
        self.kc["prepare"](build, self.cache, self.nvcc, self.cuda)  # the next build starts with none
        self.assertEqual(self.kc["read_decisions"](build), [])

    def test_prune_keeps_the_newest_two_per_kernel_and_report_lines(self):
        keys = []
        for number in range(4):
            (self.src / "util/math.h").write_text(f"float f{number}();\n", encoding="utf-8")
            keys.append(self.compile(f"b{number}")[0]["key"])
            time.sleep(0.05)
        removed = self.kc["prune"](self.cache, 2)
        self.assertEqual(len(removed), 2)
        self.assertEqual(sorted(e.name for e in self.entries()), sorted(k[:40] for k in keys[2:]))
        summary = self.kc["report"](self.work / "my build-build/b3", 0, "on")
        self.assertEqual(summary["state"], "compiled")
        self.assertTrue(
            summary["line"].startswith("GPU programs compiled")
            and summary["line"].endswith("the next Houdini build reuses them.")
        )
        self.compile("b4")
        summary = self.kc["report"](self.work / "my build-build/b4", 0, "on")
        self.assertEqual(
            summary["line"], "GPU programs reused from the first build: nothing they are made from changed."
        )
        self.assertEqual(self.kc["report"](self.work / "nothing", 0, "off")["state"], "unchanged")


if __name__ == "__main__":
    unittest.main()
