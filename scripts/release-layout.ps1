# SPDX-License-Identifier: Apache-2.0
. (Join-Path $PSScriptRoot 'project-layout.ps1')
# Release candidates and their reports: <project>\_work\releases, inside the project folder (a real folder, no link;
# docs/dev/README.md, "Working folders"). Until 22 November 2026 this was <folder>-local\releases next to the project, with
# artifacts\releases a junction to it; a real artifacts\releases folder is still read by Find-ReleasePackage.
function Get-ReleaseEvidenceRoot([string]$SourceRoot) {
    $source = Get-Item -LiteralPath $SourceRoot -ErrorAction Stop
    Get-ProjectWorkPath -Name 'releases' -SourceRoot $source.FullName
}

function Find-ReleasePackage([string]$SourceRoot, [string]$HoudiniTarget) {
    $locations = @((Get-ReleaseEvidenceRoot $SourceRoot), (Join-Path $SourceRoot 'artifacts/releases'))
    $reports = foreach ($location in $locations) {
        if (-not (Test-Path -LiteralPath $location -PathType Container)) { continue }
        Get-ChildItem -LiteralPath $location -Directory | ForEach-Object {
            Get-Item -LiteralPath (Join-Path $_.FullName 'release-report.json') -ErrorAction SilentlyContinue
        }
    }
    $candidates = foreach ($file in $reports) {
        try { $report = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop }
        catch { continue }
        if ($report.result -ne 'passed' -or $report.houdini -ne $HoudiniTarget -or $report.package_sha256 -notmatch '^[a-fA-F0-9]{64}$') { continue }
        $qualified = $file.LastWriteTimeUtc
        if ($report.finished_utc) {
            try { $qualified = ([DateTimeOffset]$report.finished_utc).UtcDateTime }
            catch { continue }
        }
        [pscustomobject]@{qualified=$qualified;package=(Join-Path $file.DirectoryName 'cycles-houdini-candidate.zip');sha256=$report.package_sha256}
    }
    $selected = $candidates | Sort-Object qualified,package -Descending | Select-Object -First 1
    if ($selected) {
        if (-not (Test-Path -LiteralPath $selected.package -PathType Leaf) -or (Get-FileHash -LiteralPath $selected.package).Hash -ne $selected.sha256) {
            throw "Release archive differs from its passing report: $($selected.package)"
        }
        return $selected.package
    }
    throw "No release package found for Houdini $HoudiniTarget. Pass -Package <zip>."
}
