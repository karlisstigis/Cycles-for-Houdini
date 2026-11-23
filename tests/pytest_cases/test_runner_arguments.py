# SPDX-License-Identifier: Apache-2.0
"""Real child argv must distinguish omitted arguments from an empty value."""

import json
import subprocess

import pytest

from support.paths import ROOT
from support.workers import ordinary_python, pure_python_environment

pytestmark = [pytest.mark.pure, pytest.mark.cpu]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("null", ["selected_case.py"]),
        ("empty", ["selected_case.py"]),
        ("planned", ["selected_case.py"]),
        ("explicit", ["selected_case.py", "--label", ""]),
    ],
)
def test_headless_launcher_preserves_actual_process_arguments(tmp_path, mode, expected):
    probe = tmp_path / "argument probe.py"
    probe.write_text("import json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    script = tmp_path / "launch.ps1"
    output = tmp_path / "argv.json"
    script.write_text("""param($Repo,$Python,$Probe,$Output,$Mode)
$ErrorActionPreference='Stop'
. (Join-Path $Repo 'scripts/native-test-context.ps1')
. (Join-Path $Repo 'scripts/native-test-tiers.ps1')
$values=switch($Mode){
    'null' {$null}
    'empty' {@()}
    'planned' {(@(Get-NativeTierCases nightly (Split-Path $Output)) | Where-Object test -eq 'blend_cache_test.py').arguments}
    'explicit' {@('--label','')}
}
$launch=@(Get-NativeHeadlessLaunchArguments -EntryPoint $Probe -Source 'selected_case.py' -Arguments $values)
$quoted=@($launch | ForEach-Object {Quote-NativeArgument $_})
$process=Start-Process -FilePath $Python -ArgumentList $quoted -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput $Output -RedirectStandardError ($Output+'.stderr')
if($process.ExitCode -ne 0){throw "Child exited $($process.ExitCode): $(Get-Content -Raw -LiteralPath ($Output+'.stderr'))"}
""")
    subprocess.run(
        [
            "pwsh",
            "-NoLogo",
            "-NoProfile",
            "-File",
            str(script),
            str(ROOT),
            str(ordinary_python()),
            str(probe),
            str(output),
            mode,
        ],
        env=pure_python_environment(),
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert json.loads(output.read_text()) == expected


def test_report_routing_requires_redirected_evidence_even_if_only_legacy_exists(tmp_path):
    legacy = tmp_path / "source/artifacts/case/report.json"
    migrated = tmp_path / "evidence/case/report.json"
    explicit = tmp_path / "explicit.json"
    legacy.parent.mkdir(parents=True)
    migrated.parent.mkdir(parents=True)
    legacy.write_text('{"result":"passed"}')
    explicit.write_text('{"result":"passed"}')
    script = tmp_path / "reports.ps1"
    script.write_text("""param($Repo,$Base)
. (Join-Path $Repo 'scripts/native-test-arguments.ps1')
$legacy=Resolve-NativeTestReport -Report 'artifacts/case/report.json' -SourceRoot (Join-Path $Base 'source') -ArtifactsRoot (Join-Path $Base 'evidence')
'{"result":"passed"}' | Set-Content -LiteralPath (Join-Path $Base 'evidence/case/report.json')
$migrated=Resolve-NativeTestReport -Report 'artifacts/case/report.json' -SourceRoot (Join-Path $Base 'source') -ArtifactsRoot (Join-Path $Base 'evidence')
$explicit=Resolve-NativeTestReport -Report (Join-Path $Base 'explicit.json') -SourceRoot (Join-Path $Base 'source') -ArtifactsRoot (Join-Path $Base 'evidence')
@($legacy,$migrated,$explicit) | ConvertTo-Json -Compress
""")
    process = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-File", str(script), str(ROOT), str(tmp_path)],
        env=pure_python_environment(),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    reports = json.loads(process.stdout)
    assert [row["Path"] for row in reports] == [str(migrated), str(migrated), str(explicit)]
    assert [row["Route"] for row in reports] == ["shared-resolver", "shared-resolver", None]


def test_native_release_routes_only_the_declared_report_argument(tmp_path):
    script = tmp_path / "release-arguments.ps1"
    script.write_text("""param($Repo,$Base)
. (Join-Path $Repo 'scripts/native-test-arguments.ps1')
$source=Join-Path $Base 'source'
$evidence=Join-Path $Base 'evidence'
$report='artifacts/iris-CPU/report.json'
$planned=Join-Path $source $report
$explicit=Join-Path $Base 'other.json'
@{
    planned=@(Get-NativeReleaseArguments @('--input',$planned,'--report',$planned) $report $source $evidence)
    explicit=@(Get-NativeReleaseArguments @('--report',$explicit) $report $source $evidence)
    omitted=@(Get-NativeReleaseArguments @('--flag') $report $source $evidence)
} | ConvertTo-Json -Compress
""")
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(script), str(ROOT), str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=pure_python_environment(),
    )
    assert json.loads(result.stdout) == {
        "planned": [
            "--input",
            str(tmp_path / "source/artifacts/iris-CPU/report.json"),
            "--report",
            str(tmp_path / "evidence/iris-CPU/report.json"),
        ],
        "explicit": ["--report", str(tmp_path / "other.json")],
        "omitted": ["--flag"],
    }
