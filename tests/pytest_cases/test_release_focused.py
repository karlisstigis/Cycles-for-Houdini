# SPDX-License-Identifier: Apache-2.0
"""Focused release metadata and failure isolation without building or loading Houdini."""

import hashlib
import json
import os
from pathlib import Path
import shutil
from struct import pack
import subprocess

import pytest

from support.paths import ROOT
from support.scenes import check_box_pixels

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.mark.parametrize("case", ["valid", "black", "flat", "nonfinite", "swapped"])
def test_quick_scene_checks_the_expected_box_and_background(case):
    red = (0.04, 0.0025, 0.00125, 1.0)
    grey = (0.05, 0.05, 0.05, 1.0)
    pixels = []
    for y in range(24):
        for x in range(32):
            interior = 12 <= x < 20 and 8 <= y < 16
            color = red if interior else grey
            if case == "black":
                color = (0.0,) * 4
            elif case == "flat":
                color = red
            elif case == "nonfinite" and (x, y) == (4, 5):
                color = (float("nan"), 0.0, 0.0, 1.0)
            elif case == "swapped":
                color = grey if interior else red
            pixels.append(pack("<4f", *color))
    data = b"".join(pixels)
    if case == "valid":
        assert check_box_pixels(data)["interior_rgb"] == pytest.approx(red[:3])
    else:
        with pytest.raises(AssertionError):
            check_box_pixels(data)


def test_standalone_installer_selects_matching_passed_release(tmp_path):
    source = tmp_path / "Artist Project"
    source.mkdir()
    evidence = source / "_work" / "releases"
    for index, (directory, status, version) in enumerate(
        [
            ("old", "passed", "22.0.466"),
            ("new", "passed", "22.0.466"),
            ("failed", "failed", "22.0.466"),
            ("other", "passed", "22.0.429"),
        ]
    ):
        location = source / "artifacts/releases" if directory == "new" else evidence
        candidate = location / directory / "cycles-houdini-candidate.zip"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(directory.encode())
        report = candidate.with_name("release-report.json")
        report.write_text(
            json.dumps(
                dict(result=status, houdini=version, package_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest())
            )
        )
        os.utime(report, (1600000000 + index, 1600000000 + index))
    partial = evidence / "in-progress" / "release-report.json"
    partial.parent.mkdir()
    partial.write_text("{")
    probe = tmp_path / "resolve.ps1"
    probe.write_text(
        "param($Layout,$SourceRoot)\n$ErrorActionPreference='Stop'\n. $Layout\n"
        "Find-ReleasePackage $SourceRoot '22.0.466'\n"
    )
    command = ["pwsh", "-NoProfile", "-File", str(probe), str(ROOT / "scripts/release-layout.ps1"), str(source)]
    expected = source / "artifacts/releases/new/cycles-houdini-candidate.zip"
    assert Path(subprocess.check_output(command, text=True).strip()) == expected
    expected.write_bytes(b"changed after qualification")
    failure = subprocess.run(command, capture_output=True, text=True)
    assert failure.returncode != 0 and "Release archive differs" in failure.stderr
    for release_report in tmp_path.rglob("release-report.json"):
        release_report.unlink()
    legacy = source / "artifacts/old-candidate/cycles-houdini-candidate.zip"
    legacy.parent.mkdir()
    legacy.write_bytes(b"unqualified")
    missing = subprocess.run(command, capture_output=True, text=True)
    assert missing.returncode != 0 and "Pass -Package" in missing.stderr


@pytest.mark.parametrize(
    "mode,expected", [("passed", "passed"), ("child-failed", "failed"), ("manifest-changed", "failed")]
)
def test_focused_fixture_preserves_manifest_scope_counts_and_environment(tmp_path, mode, expected):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "output").mkdir()
    (tmp_path / "build/build-manifest.json").write_text('{"identity":"selected"}')
    (
        scripts / "run-prototype-test.ps1"
    ).write_text(r"""param($Test,$Device,$Execution,$HoudiniRoot,$BuildDirectory,$PackageDirectory,$UserPrefDirectory,$TimeoutSeconds,$Report,$TestArguments,[switch]$Gui)
@{result='passed';artifacts=$env:CYCLES_HOUDINI_TEST_ARTIFACTS;worker_logs=$env:CYCLES_HOUDINI_WORKER_LOG_DIR;device=$Device} | ConvertTo-Json | Set-Content -LiteralPath $Report
if($env:CH_FOCUSED_TEST_MODE -eq 'manifest-changed'){Set-Content -LiteralPath (Join-Path $BuildDirectory 'build-manifest.json') -Value '{"identity":"changed"}'}
if($env:CH_FOCUSED_TEST_MODE -eq 'child-failed'){exit 9}
exit 0
""")
    harness = tmp_path / "exercise.ps1"
    harness.write_text(r"""param($Repo,$Base,$Mode)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $Repo 'scripts/build-release.ps1'),[ref]$tokens,[ref]$errors)
if($errors.Count){throw $errors[0]}
$functions=$ast.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('Save-ReleaseReport','Assert-ReleaseManifest','Invoke-ReleaseTest')},$true)
($functions | ForEach-Object {$_.Extent.Text}) -join "`n" | Set-Content -LiteralPath (Join-Path $Base 'scripts/functions.ps1')
. (Join-Path $Base 'scripts/functions.ps1')
$BuildDirectory=Join-Path $Base 'build';$OutputDirectory=Join-Path $Base 'output';$HoudiniRoot=Join-Path $Base 'fake-hfs'
$report=[ordered]@{result='running';checks=@();native_tests=@();qualification_mode='release+focused';full_nightly_qualified=$false;nightly_case_count=0;tested_manifest_sha256=(Get-FileHash (Join-Path $BuildDirectory 'build-manifest.json')).Hash.ToLowerInvariant()}
$env:CH_FOCUSED_TEST_MODE=$Mode
$env:CYCLES_HOUDINI_TEST_ARTIFACTS='previous-artifacts';$env:CYCLES_HOUDINI_WORKER_LOG_DIR='previous-worker-logs'
try{
    Invoke-ReleaseTest 'focused-one' 'selected_contract.py' 'selected-packages' @() (Join-Path $OutputDirectory 'case.json') -Device 'CPU' -Focused
    $report.result='passed'
}catch{$report.result='failed';$report.error=$_.Exception.Message}
Save-ReleaseReport
@{report=$report;restored_artifacts=$env:CYCLES_HOUDINI_TEST_ARTIFACTS;restored_worker_logs=$env:CYCLES_HOUDINI_WORKER_LOG_DIR;child=(Get-Content (Join-Path $OutputDirectory 'case.json') -Raw | ConvertFrom-Json)} | ConvertTo-Json -Depth 24 -Compress
""")
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(harness), str(ROOT), str(tmp_path), mode],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    evidence = json.loads(result.stdout)
    report = evidence["report"]
    assert report["result"] == expected
    assert report["native_tests"][0]["result"] == expected
    assert report["native_fixture_invocations"] == report["focused_fixture_invocations"] == 1
    assert report["focused_passed_fixture_invocations"] == (1 if expected == "passed" else 0)
    assert report["full_nightly_qualified"] is False and report["nightly_case_count"] == 0
    assert evidence["restored_artifacts"] == "previous-artifacts"
    assert evidence["restored_worker_logs"] == "previous-worker-logs"
    assert Path(evidence["child"]["artifacts"]) == tmp_path / "output/focused-one/artifacts"
    assert Path(evidence["child"]["worker_logs"]) == tmp_path / "output/focused-one/artifacts/workers"
    if mode == "manifest-changed":
        assert "manifest changed" in report["error"]


@pytest.mark.parametrize("picker", [False, True])
def test_official_launcher_dry_run_validates_inputs_without_creating_folders(tmp_path, picker):
    source = tmp_path / "Official Source Fixture"
    scripts, launchers = source / "scripts", source / "launchers"
    scripts.mkdir(parents=True)
    launchers.mkdir()
    script_names = ("build-release.ps1", "build-layout.ps1", "release-layout.ps1", "project-layout.ps1")
    for name in script_names:
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    shutil.copyfile(ROOT / "source.lock.json", source / "source.lock.json")
    (scripts / "engine-fork.py").write_text("print('private')\n")
    launcher = launchers / "Build and make installer.cmd"
    shutil.copyfile(ROOT / "launchers" / launcher.name, launcher)

    target = json.loads((source / "source.lock.json").read_text())["certification_candidate"]
    sdk = tmp_path / "Fixture SDK"
    headers = sdk / "toolkit/include"
    (headers / "SYS").mkdir(parents=True)
    (headers / "UT").mkdir()
    (headers / "SYS/SYS_Version.h").write_text(f'#define SYS_VERSION_FULL "{target["houdini"]}"\n')
    (headers / "UT/UT_HDKVersion.h").write_text(f"#define HDK_API_VERSION {target['houdini_api']}\n")
    hip = tmp_path / "Existing Fixture.hiplc"
    hip.write_bytes(b"fixture path only")
    build, output = tmp_path / "New Build", tmp_path / "New Output"
    args = [
        "-HoudiniRoot",
        str(sdk),
        "-DryRun",
        "-ExistingHip",
        str(hip),
        "-BuildDirectory",
        str(build),
        "-OutputDirectory",
        str(output),
    ]
    if picker:
        args.append("-ChooseRollbackPackage")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "call", str(launcher), *args],
        capture_output=True,
        text=True,
        input="",
        timeout=15,
    )
    assert not build.exists() and not output.exists()
    assert "Press any key" not in (result.stdout + result.stderr)
    if picker:
        assert result.returncode != 0 and "cannot open the package chooser" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        plan = json.loads(result.stdout)
        assert plan["result"] == "planned" and plan["engine_route"] == "private"
        assert plan["houdini"] == target["houdini"] and Path(plan["existing_hip"]) == hip
        assert Path(plan["build_directory"]) == build and Path(plan["output_directory"]) == output


def test_focused_path_keeps_normal_release_and_independent_nightly_gates():
    source = (ROOT / "scripts/build-release.ps1").read_text()
    assert "[switch]$FocusedChecks" in source
    assert "if($QualifiedNightly)" in source and "'run-nightly.py'),'verify'" in source
    assert "expected_focused_fixture_invocations=$(if($FocusedChecks){8}else{0})" in source
    for script in (
        "houdini_release_runtime_smoke.py",
        "houdini_gaussian_splats_smoke.py",
        "houdini_gsplats_dlss_worker_smoke.py",
        "houdini_saved_hip_farm_smoke.py",
        "houdini_farm_output_policy_smoke.py",
        "houdini_release_response_gui_smoke.py",
    ):
        assert script in source
    for gate in ("payload-preservation", "source-unchanged", "patch-replay-final", "isolated-uninstall"):
        assert gate in source
    assert "$installer.package_sha256 -ne $report.package_sha256" in source


def test_asset_browser_is_an_optional_addon_gated_by_the_installer():
    """The add-on lives outside the always-loaded host paths; Setup and staging carry it."""
    import xml.etree.ElementTree as ET

    addon = ROOT / "houdini/addons/asset_browser"
    panel = ET.parse(addon / "python_panels/cycles_assets.pypanel").find("interface")
    assert panel.get("name") == "cycles_houdini_assets" and panel.get("label") == "Cycles Asset Browser"
    script = panel.find("script").text
    assert "from cycles_houdini.assets.panel import AssetBrowserPanel" in script and "dispose()" in script
    assert panel.find("includeInPaneTabMenu") is not None and panel.find("includeInToolbarMenu") is not None
    # The add-on composes its tool into the host shelf when Houdini loads the add-on path.
    shelf = ET.parse(addon / "toolbar/CyclesAssets.shelf").getroot()
    assert shelf.find("toolshelf") is None
    assert [tool.get("name") for tool in shelf.findall("tool")] == ["cycles_asset_browser"]
    assert shelf.find("tool").get("icon") == "cycles_asset_browser"
    assert (addon / "config/Icons/cycles_asset_browser.svg").is_file()
    assert "cycles_houdini_assets" in shelf.find("tool/script").text
    host_shelf = ET.parse(ROOT / "houdini/toolbar/Cycles.shelf").getroot()
    member_names = [t.get("name") for t in host_shelf.findall("toolshelf/memberTool")]
    assert "cycles_asset_browser" not in member_names
    assert set(member_names) <= {t.get("name") for t in host_shelf.findall("tool")}
    edits = shelf.findall("toolshelfEdit")
    assert len(edits) == 1
    assert edits[0].attrib == dict(
        name=host_shelf.find("toolshelf").get("name"), fileLocation="$CYCLES_HOUDINI_HOST_ROOT/toolbar/Cycles.shelf"
    )
    assert [(child.tag, child.attrib) for child in edits[0]] == [
        ("addMemberTool", dict(name="cycles_asset_browser", inPosition=str(len(member_names))))
    ]
    # Host menus: the node right-click and network editor Cycles menus are host menus (shown with the
    # Blender bridge); the add-on keeps only Cycles > Asset Browser.
    opmenu = ET.parse(ROOT / "houdini/OPmenu.xml").getroot().find("menu/subMenu")
    assert opmenu.get("id") == "cycles_asset_link_menu"
    assert {item.get("id") for item in opmenu.findall("scriptItem")} == {
        "cycles_convert_material",
        "cycles_node_keep_current_graph",
        "cycles_node_collect_textures",
        "cycles_asset_append_linked",
        "cycles_asset_reload_linked",
    }
    network_menu = ET.parse(ROOT / "houdini/NetworkViewMenu.xml").getroot().find("menuBar/subMenu")
    assert network_menu.get("id") == "cycles_network_view_menu"
    main_menu = ET.parse(addon / "MainMenuCommon.xml").getroot().find("menuBar/subMenu")
    assert main_menu.get("id") == "cycles_main_menu" and main_menu.find("modifyItem/insertBefore") is not None
    assert [item.get("id") for item in main_menu.findall("scriptItem")] == ["cycles_main_asset_browser"]
    for name in ("OPmenu.xml", "NetworkViewMenu.xml"):
        assert not (addon / name).exists()
    # The optional panel and its menu command live with the optional shelf tool.
    for path in ("houdini/MainMenuCommon.xml", "houdini/OPmenu.xml", "houdini/NetworkViewMenu.xml"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "cycles_asset_browser" not in text and "cycles_houdini_assets" not in text
    main_text = (ROOT / "houdini/MainMenuCommon.xml").read_text(encoding="utf-8")
    assert "cycles_asset_link_menu" not in main_text and "cycles_network_view_menu" not in main_text
    assert "cycles_houdini_assets" not in (ROOT / "houdini/toolbar/Cycles.shelf").read_text(encoding="utf-8")
    assert not list((ROOT / "houdini/python_panels").glob("cycles_assets*"))

    stage = (ROOT / "scripts/stage-development.ps1").read_text(encoding="utf-8")
    assert "'help','addons')" in stage and '"$hostRoot/addons/asset_browser"' in stage
    # The host menu files are staged into the host root (and so into the release package).
    assert "@('MainMenuCommon.xml','OPmenu.xml','NetworkViewMenu.xml')" in stage
    setup = (ROOT / "packaging/installer/CyclesHoudini.iss").read_text(encoding="utf-8")
    assert 'Name: "blenderbridge\\assetbrowser";' in setup and "Flags: checkablealone" in setup
    assert "' --asset-browser enabled'" in setup and "' --asset-browser disabled'" in setup
    assert "--asset-browser" in (ROOT / "scripts/build-installer.ps1").read_text(encoding="utf-8")
    for launcher in ("launch-normal-build.ps1", "launch-normal-test.ps1"):
        text = (ROOT / "scripts" / launcher).read_text(encoding="utf-8")
        assert "[switch]$WithoutAssetBrowser" in text and "New-PackageDirectoryWithoutAssetBrowser" in text


def test_blend_importer_menu_and_shelf_contract():
    """File > Import > Blender (.blend), Cycles > Import Blender, and the Import .blend shelf tool."""
    import xml.etree.ElementTree as ET

    from cycles_houdini.blender import blend_import_dialog as dialog

    raw = (ROOT / "houdini/MainMenuCommon.xml").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
    root = ET.fromstring(raw)
    added = root.findall("menu/addScriptItem")
    assert [item.get("id") for item in added] == ["cycles_import_blend"]
    [item] = added
    # One constant names Houdini's Import submenu and the anchor item; the XML must match it.
    assert item.find("parent").text == dialog.FILE_IMPORT_MENU_ID == "import_files"
    assert item.find("insertAfter").text == dialog.FILE_IMPORT_INSERT_AFTER == "h.import_gltf"
    assert item.find("label").text == "Blender (.blend)..."
    host = root.find("menuBar/subMenu")
    [main_item] = [entry for entry in host.findall("scriptItem") if entry.get("id") == "cycles_main_import_blend"]
    assert main_item.find("label").text == "Import Blender (.blend)..."
    for entry in (item, main_item):
        # Houdini evaluates main-menu contexts once at startup: a one-line expression (no return).
        expression = entry.find("context/expression").text.strip()
        assert "\n" not in expression and "return" not in expression
        assert "cycles_houdini.core.blender_bridge" in expression and expression.endswith(".enabled()")
        compile(expression, "MainMenuCommon.xml", "eval")
        assert "blend_import_dialog.open_dialog()" in entry.find("scriptCode").text
    # The importer is host-only: the asset browser add-on does not define it.
    addon_menu = (ROOT / "houdini/addons/asset_browser/MainMenuCommon.xml").read_text(encoding="utf-8")
    assert "cycles_import_blend" not in addon_menu and "blend_import_dialog" not in addon_menu

    shelf_bytes = (ROOT / "houdini/toolbar/Cycles.shelf").read_bytes()
    assert shelf_bytes.count(b"\r\n") == shelf_bytes.count(b"\n")
    shelf = ET.fromstring(shelf_bytes)
    members = [tool.get("name") for tool in shelf.findall("toolshelf/memberTool")]
    assert members.index("cycles_import_blend") == members.index("cycles_material_preview") + 1
    [tool] = [tool for tool in shelf.findall("tool") if tool.get("name") == "cycles_import_blend"]
    assert tool.get("label") == "Import .blend" and tool.get("icon") == "cycles_import_blend"
    assert "blend_import_dialog.open_dialog()" in tool.find("script").text
    icon = (ROOT / "houdini/config/Icons/cycles_import_blend.svg").read_text(encoding="utf-8")
    assert 'viewBox="-2 -2 28 28"' in icon and 'rx="5.6" fill="#3A3A3A"' in icon
    assert "cycles_import_blend.svg" in (ROOT / "houdini/config/Icons/CYCLES_TOOLS_NOTICE.txt").read_text(
        encoding="utf-8"
    )
