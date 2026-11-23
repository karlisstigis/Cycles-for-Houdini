# SPDX-License-Identifier: Apache-2.0
# "I changed the engine": saves your edits in third_party\cycles as new files in patches\ and
# updates source.lock.json, so that a fresh clone rebuilds the same engine. Asks before it
# commits anything. Safe to run again: with nothing new it says so and changes nothing.
# Details: scripts/update-engine-patches.py --help.
[CmdletBinding()]
param([string]$Message = '', [switch]$Yes)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'easy-common.ps1')
trap { Write-EasyCrash $_; exit 1 }
Write-EasyTitle 'Update engine patches (after changing the engine)'
$python = Find-EasyPython
if (-not (Find-EasyGit) -or -not $python) { Write-EasyResult $false 'Git or Python is missing.' ((Get-EasyAgain 'Check this computer again') + '.'); exit 1 }
$arguments = @()
if ($Message) { $arguments += @('--message', $Message) }
if ($Yes -or $env:CYCLES_EASY_ANSWER -eq 'yes') { $arguments += '--yes' }
& $python -I (Join-Path $PSScriptRoot 'update-engine-patches.py') @arguments
if ($LASTEXITCODE -ne 0) {
    Write-EasyResult $false 'The engine patches were not updated (the reason is above; "already_done" says what was saved before it stopped).' 'Fix what the message says, then run this again: it continues from where it stopped.'
    exit 1
}
Write-EasyResult $true 'patches\ and source.lock.json now describe your engine (see the list above).' ((Get-EasyAgain 'Update the sources and rebuild') + ' (it also runs the quick tests).')
exit 0
