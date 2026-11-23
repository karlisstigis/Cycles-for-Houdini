# SPDX-License-Identifier: Apache-2.0
function Get-StagingRuntimeFiles([string]$Directory) {
    $files = @(Get-ChildItem -LiteralPath $Directory -Recurse -File -ErrorAction Stop |
        Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' })
    if (-not $files.Count) { throw "Empty runtime directory: $Directory" }
    $files.FullName
}
