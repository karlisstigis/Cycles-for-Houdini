# SPDX-License-Identifier: Apache-2.0
# Windows PowerShell 5.1 peer of release-package.py for registration recovery and uninstall.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Recover','Uninstall')][string]$Action,
    [Parameter(Mandatory=$true)][string]$ApplicationRoot,
    [Parameter(Mandatory=$true)][guid]$AppId,
    [string]$Log,
    [switch]$WizardLeaseHeld
)

$ErrorActionPreference = 'Stop'
$MarkerName = '.cycles-houdini-installation.json'
$Utf8 = [Text.UTF8Encoding]::new($false, $true)

function Require($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Get-SafePath([string]$Path) {
    Require ([IO.Path]::IsPathRooted($Path)) "Installation path is not absolute: $Path"
    $resolved = [IO.Path]::GetFullPath($Path)
    for ($item = $resolved; $item; $item = [IO.Path]::GetDirectoryName($item)) {
        try { $attrs = [IO.File]::GetAttributes($item) }
        catch [IO.FileNotFoundException] { continue }
        catch [IO.DirectoryNotFoundException] { continue }
        Require (-not ($attrs -band [IO.FileAttributes]::ReparsePoint)) "Reparse path is not allowed: $item"
    }
    return $resolved
}

function Test-Overlap([string]$First, [string]$Second) {
    $a = $First.TrimEnd('\') + '\'
    $b = $Second.TrimEnd('\') + '\'
    return $a.StartsWith($b, [StringComparison]::OrdinalIgnoreCase) -or
        $b.StartsWith($a, [StringComparison]::OrdinalIgnoreCase)
}

function Read-Snapshot([string]$Path) {
    $file = Get-SafePath $Path
    try { return [Convert]::ToBase64String([IO.File]::ReadAllBytes($file)) }
    catch [IO.FileNotFoundException] { return $null }
    catch [IO.DirectoryNotFoundException] { return $null }
}

function Write-Snapshot([string]$Path, $Snapshot) {
    $file = Get-SafePath $Path
    if ($null -eq $Snapshot) {
        if ([IO.File]::Exists($file)) { [IO.File]::Delete($file) }
        return
    }
    $bytes = [Convert]::FromBase64String($Snapshot)
    $parent = [IO.Path]::GetDirectoryName($file)
    $null = [IO.Directory]::CreateDirectory($parent)
    $scratch = Join-Path $parent ('.' + [IO.Path]::GetFileName($file) + '.' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $stream = [IO.File]::Open($scratch, 'CreateNew', 'Write', 'Read')
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        } finally { $stream.Dispose() }
        if ([IO.File]::Exists($file)) {
            [IO.File]::Replace($scratch, $file, [NullString]::Value)
        } else {
            [IO.File]::Move($scratch, $file)
        }
    } finally {
        if ([IO.File]::Exists($scratch)) { [IO.File]::Delete($scratch) }
    }
}

function Read-JsonSnapshot($Snapshot) {
    if ($null -eq $Snapshot) { return $null }
    $text = $Utf8.GetString([Convert]::FromBase64String($Snapshot)).TrimStart([char]0xFEFF)
    return ConvertFrom-Json -InputObject $text
}

function Format-Json($Record) {
    return ConvertTo-Json -InputObject $Record -Depth 32 -Compress
}

function Save-JsonSnapshot($Record) {
    if ($null -eq $Record) { return $null }
    return [Convert]::ToBase64String($Utf8.GetBytes((Format-Json $Record) + [char]10))
}

function Get-SnapshotHash($Snapshot) {
    if ($null -eq $Snapshot) { return $null }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash([Convert]::FromBase64String($Snapshot))
        return [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Open-InstallationLock([string]$Directory) {
    $lockPath = Get-SafePath (Join-Path $Directory '.installation.lock')
    $stream = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'ReadWrite')
    try {
        $stream.Lock(0, 1)
        if ($stream.Length -eq 0) {
            $stream.WriteByte(48)
            $stream.Flush()
        }
        return $stream
    } catch {
        $stream.Dispose()
        throw 'Another installation operation holds the installation lock.'
    }
}

function Get-Owner([string]$Root) {
    $owner = Read-JsonSnapshot (Read-Snapshot (Join-Path $Root $MarkerName))
    Require ($owner.schema_version -eq 1 -and $owner.product -ceq 'Cycles for Houdini' -and
        $owner.installation_id -cmatch '^[0-9a-f]{32}$') 'Invalid installation ownership marker.'
    return $owner
}

function Get-RegistrationName($Owner) {
    Require ($Owner.houdini -cmatch '^22\.0\.\d+$') 'Invalid Houdini target in the installation receipt.'
    $version = $Owner.houdini.Replace('.', '_')
    if ($null -eq $Owner.build_id) { return "cycles_houdini_$version.json" }
    Require ($Owner.build_id -cmatch '^[a-z][a-z0-9-]{1,30}[a-z0-9]$') 'Invalid community build ID in the receipt.'
    $build = $Owner.build_id.Replace('-', '_')
    return ('cycles_houdini_community_' + $build + '_' + $version + '.json')
}

function Confirm-StateOwner($Location, $State) {
    Require ($State.schema_version -eq 1 -and $State.active -cmatch '^[0-9a-f]{64}$' -and
        $State.houdini -ceq $Location.Owner.houdini -and $State.app_id -ceq $Location.Owner.app_id -and
        $State.build_identity.build_id -ceq $Location.Owner.build_id -and
        $State.package_filename -ceq $Location.Owner.package_filename -and
        $State.package_path -ceq $Location.Registration -and
        $State.preference_root -ceq $Location.Preferences) 'Installation state differs from its ownership marker.'
}

function Confirm-ActiveState($Location, $State, $Snapshot) {
    Confirm-StateOwner $Location $State
    Require ($null -ne $Snapshot -and (Get-SnapshotHash $Snapshot) -ceq $State.package_sha256) (
        'Configuration was changed externally; preserving it.'
    )
}

function Get-TransactionChanges($Location, $Transaction) {
    $owner = $Location.Owner
    Require ($Transaction.schema_version -eq 1 -and
        $Transaction.operation -cin @('install','refresh-registration','uninstall','migrate-legacy') -and
        $Transaction.installation_id -ceq $owner.installation_id -and $Transaction.houdini -ceq $owner.houdini -and
        $Transaction.package_filename -ceq $owner.package_filename -and
        $Transaction.preference_root -ceq $Location.Preferences -and
        $Transaction.package_path -ceq $Location.Registration) 'Transaction belongs to another installation or target.'
    Require ((Get-SnapshotHash $Transaction.intended) -ceq $Transaction.owned_sha256) 'Invalid registration digest in the transaction.'
    if ($Transaction.previous_state.active) {
        Confirm-ActiveState $Location $Transaction.previous_state $Transaction.previous
    }
    if ($Transaction.operation -ceq 'uninstall') {
        Require ($Transaction.previous_state.active -and -not $Transaction.intended_state.active -and
            $Transaction.previous_state.original_package -ceq $Transaction.intended) 'Invalid uninstall receipt.'
    } else {
        Confirm-ActiveState $Location $Transaction.intended_state $Transaction.intended
    }

    $extras = @($Transaction.extra_changes | Where-Object { $null -ne $_ })
    if ($Transaction.operation -ceq 'uninstall' -and $Transaction.previous_state.legacy_backup) {
        Require ($extras.Count -eq 1 -and $null -eq $extras[0].previous -and
            $extras[0].intended -ceq $Transaction.previous_state.legacy_backup.original_package -and
            $Transaction.previous_state.legacy_backup.path -ceq $Location.LegacyRegistration) 'Invalid legacy restore receipt.'
    } elseif ($Transaction.operation -ceq 'migrate-legacy') {
        Require ($extras.Count -eq 2 -and $null -eq $extras[0].intended) 'Invalid legacy migration receipt.'
    } else {
        Require ($extras.Count -eq 0) 'Unexpected transaction side effects.'
    }
    if ($extras.Count) {
        Require ($extras[0].path -ceq $Location.LegacyRegistration) 'Transaction side effect escapes the owned registration.'
    }
    return $extras
}

function Confirm-LegacyMigration($Location, $Transaction, $Changes, [string]$LegacyRoot) {
    $legacy = Get-Owner $LegacyRoot
    $before = Read-JsonSnapshot $Changes[1].previous
    $after = Read-JsonSnapshot $Changes[1].intended
    Require ($legacy.installation_id -ceq $Transaction.legacy_owner.installation_id -and
        $Changes[1].path -ceq (Join-Path $LegacyRoot 'state.json') -and
        $before.active -cmatch '^[0-9a-f]{64}$' -and $before.package_path -ceq $Location.LegacyRegistration -and
        (Get-SnapshotHash $Changes[0].previous) -ceq $before.package_sha256 -and
        -not $after.active -and $after.migrated_to -ceq $Location.Root) 'Invalid legacy migration owner or state.'
    $version = Get-SafePath (Join-Path $LegacyRoot ('versions\' + $before.active))
    if (-not [IO.Directory]::Exists($version)) {
        $version = Get-SafePath (Join-Path $LegacyRoot ('versions\' + $before.active.Substring(0, 16)))
    }
    $manifestBytes = Read-Snapshot (Join-Path $version 'release-manifest.json')
    $manifest = Read-JsonSnapshot $manifestBytes
    Require ($manifest.package_id -ceq $before.active -and $manifest.houdini -ceq $Location.Owner.houdini) (
        'Legacy package identity changed after interruption; preserving it.'
    )
    if ($Transaction.legacy_owner.manifest_sha256) {
        Require ((Get-SnapshotHash $manifestBytes) -ceq $Transaction.legacy_owner.manifest_sha256) (
            'Legacy package manifest changed after interruption; preserving it.'
        )
    }
}

function Write-Transaction($Location, [string]$Path, $Transaction, $Changes, [string]$Direction) {
    $current = Read-Snapshot $Location.State
    $before = $Transaction.previous_state_bytes
    $after = $Transaction.intended_state_bytes
    $stateProblem = 'Installation state changed after interruption; preserving it.'
    if ($Transaction.PSObject.Properties['previous_state_bytes']) {
        Require ((Format-Json (Read-JsonSnapshot $before)) -ceq (Format-Json $Transaction.previous_state) -and
            (Format-Json (Read-JsonSnapshot $after)) -ceq (Format-Json $Transaction.intended_state)) (
            'Transaction state bytes differ from its receipt.'
        )
        Require ($current -ceq $before -or $current -ceq $after) $stateProblem
    } else {
        # Older journals recorded state objects and the exact registration snapshots.
        $json = Format-Json (Read-JsonSnapshot $current)
        Require ($json -ceq (Format-Json $Transaction.previous_state) -or
            $json -ceq (Format-Json $Transaction.intended_state)) $stateProblem
        $before = Save-JsonSnapshot $Transaction.previous_state
        $after = Save-JsonSnapshot $Transaction.intended_state
    }
    $registration = [pscustomobject]@{
        path = $Location.Registration; previous = $Transaction.previous; intended = $Transaction.intended
    }
    $files = @($registration) + $Changes
    $fileProblem = 'Configuration changed after interruption; refusing to overwrite it.'
    foreach ($change in $files) {
        $actual = Read-Snapshot $change.path
        Require ($actual -ceq $change.previous -or $actual -ceq $change.intended) $fileProblem
        if ($null -ne $change.$Direction) { $null = [Convert]::FromBase64String($change.$Direction) }
    }
    if ($Direction -ceq 'previous') {
        [Array]::Reverse($files)
        $state = $before
    } else {
        $state = $after
        Write-Snapshot $Path (Save-JsonSnapshot $Transaction)
    }
    foreach ($change in $files) {
        $actual = Read-Snapshot $change.path
        Require ($actual -ceq $change.previous -or $actual -ceq $change.intended) $fileProblem
        if ($actual -cne $change.$Direction) { Write-Snapshot $change.path $change.$Direction }
    }
    Require ((Read-Snapshot $Location.State) -ceq $current) $stateProblem
    if ($current -cne $state) { Write-Snapshot $Location.State $state }
    $Transaction.state = if ($Direction -ceq 'previous') { 'rolled_back' } else { 'committed' }
    Write-Snapshot $Path (Save-JsonSnapshot $Transaction)
}

function Get-PendingTransactions([string]$Root) {
    $directory = Get-SafePath (Join-Path $Root 'transactions')
    if (-not [IO.Directory]::Exists($directory)) { return }
    Get-ChildItem -LiteralPath $directory -Filter '*.json' -File | ForEach-Object {
        $record = Read-JsonSnapshot (Read-Snapshot $_.FullName)
        if ($record.state -cnotin @('committed','rolled_back')) {
            [pscustomobject]@{ Path = $_.FullName; Record = $record }
        }
    }
}

function Repair-Registration($Location) {
    $pending = @(Get-PendingTransactions $Location.Root)
    if (-not [IO.Directory]::Exists($Location.Preferences) -and $pending.Count) {
        throw 'The Houdini preferences folder is missing and an installation was interrupted. Restore that folder from your backup, then run Setup again.'
    }
    Require ($pending.Count -le 1) 'Multiple unfinished installation transactions; preserving them for inspection.'
    foreach ($entry in $pending) {
        $transaction = $entry.Record
        Require ($transaction.state -cin @('prepared','setup_pending')) 'Invalid interrupted transaction status.'
        $changes = @(Get-TransactionChanges $Location $transaction)
        $legacyLock = $null
        try {
            if ($transaction.operation -ceq 'migrate-legacy') {
                $legacyRoot = Get-SafePath $transaction.legacy_owner.root
                Require (-not (Test-Overlap $Location.Application $legacyRoot) -and
                    -not (Test-Overlap $Location.Preferences $legacyRoot)) 'Legacy owner overlaps this installation.'
                $null = Get-Owner $legacyRoot
                $legacyLock = Open-InstallationLock $legacyRoot
                Confirm-LegacyMigration $Location $transaction $changes $legacyRoot
            }
            Write-Transaction $Location $entry.Path $transaction $changes 'previous'
        } finally {
            if ($legacyLock) { $legacyLock.Dispose() }
        }
    }
}

function Remove-Registration($Location) {
    $before = Read-Snapshot $Location.State
    $state = Read-JsonSnapshot $before
    if (-not $state.active) { return }
    Confirm-StateOwner $Location $state
    $current = Read-Snapshot $Location.Registration
    $after = [pscustomobject][ordered]@{ active = $null; houdini = $state.houdini; schema_version = 1 }
    if ($null -eq $current) {
        # Someone already removed the registration. Retire only the receipt.
        # Restoring a saved package here would recreate their removed settings.
        Require ((Read-Snapshot $Location.State) -ceq $before) 'Installation state changed; preserving it.'
        Write-Snapshot $Location.State (Save-JsonSnapshot $after)
        return
    }
    Confirm-ActiveState $Location $state $current
    $extras = @()
    if ($state.legacy_backup) {
        $extras = @([pscustomobject]@{
            path = $Location.LegacyRegistration; previous = $null; intended = $state.legacy_backup.original_package
        })
        Require ($null -eq (Read-Snapshot $Location.LegacyRegistration)) 'Legacy registration changed after migration; preserving it.'
    }
    # Python reads the community identity from previous_state when the journal has no build_identity field.
    $transaction = [pscustomobject][ordered]@{
        schema_version = 1; operation = 'uninstall'; state = 'prepared'; setup_owned = $false; setup_token = $null
        installation_id = $Location.Owner.installation_id
        package_path = $Location.Registration; preference_root = $Location.Preferences
        package_filename = $Location.Owner.package_filename; houdini = $Location.Owner.houdini
        owned_sha256 = Get-SnapshotHash $state.original_package
        previous = $current; intended = $state.original_package
        previous_state = $state; intended_state = $after
        previous_state_bytes = $before; intended_state_bytes = Save-JsonSnapshot $after
        extra_changes = $extras
    }
    $changes = @(Get-TransactionChanges $Location $transaction)
    $path = Join-Path $Location.Root ('transactions\' + [guid]::NewGuid().ToString('N') + '.json')
    Write-Transaction $Location $path $transaction $changes 'intended'
}

function Invoke-Maintenance {
    $app = Get-SafePath $ApplicationRoot
    $root = Get-SafePath (Join-Path $app 'installation')
    if ($null -eq (Read-Snapshot (Join-Path $root $MarkerName))) {
        if ([IO.Directory]::Exists($root) -and @([IO.Directory]::EnumerateFileSystemEntries($root)).Count) {
            throw 'Installation directory is not owned by Cycles for Houdini.'
        }
        return
    }
    $owner = Get-Owner $root
    if ($Action -ceq 'Recover' -and -not @(Get-PendingTransactions $root).Count) { return }
    Require ($owner.application_root -ceq $app -and $owner.app_id -ieq $AppId.ToString('B')) 'Installation belongs to another application.'
    $prefs = Get-SafePath $owner.preference_root
    Require (-not (Test-Overlap $prefs $app) -and $prefs -ine [IO.Path]::GetPathRoot($prefs)) 'Invalid installation preferences root.'
    $filename = Get-RegistrationName $owner
    Require ($owner.package_filename -ceq $filename) 'Invalid stored package filename.'
    $location = [pscustomobject]@{
        Owner = $owner; Application = $app; Root = $root; Preferences = $prefs
        Registration = Get-SafePath (Join-Path $prefs ('packages\' + $filename))
        LegacyRegistration = Get-SafePath (Join-Path $prefs 'packages\cycles_houdini_dev.json')
        State = Get-SafePath (Join-Path $root 'state.json')
    }
    $prefLock = $null
    try {
        if ([IO.Directory]::Exists($prefs)) { $prefLock = Open-InstallationLock $prefs }
        $rootLock = Open-InstallationLock $root
        try {
            Require ($prefLock -or -not [IO.Directory]::Exists($prefs)) 'The preferences folder appeared during uninstall. Retry to check its registration.'
            Require ((Format-Json (Get-Owner $root)) -ceq (Format-Json $owner)) 'Installation ownership changed during preparation.'
            Repair-Registration $location
            if ($Action -ceq 'Uninstall') { Remove-Registration $location }
        } finally { $rootLock.Dispose() }
    } finally {
        if ($prefLock) { $prefLock.Dispose() }
    }
}

$lease = $null
$leaseHeld = $false
$exitCode = 0
try {
    # Setup and its Windows uninstaller hold this mutex while they call the helper.
    if (-not $WizardLeaseHeld) {
        $lease = [Threading.Mutex]::new($false, ('Global\CyclesForHoudiniInstaller-' + $AppId.ToString('B').ToUpperInvariant()))
        try { $leaseHeld = $lease.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $leaseHeld = $true }
        Require $leaseHeld 'An installer or uninstaller is already working on this Cycles for Houdini build.'
    }
    Invoke-Maintenance
    $message = "$Action completed."
} catch {
    $message = $_.Exception.Message
    $exitCode = 1
} finally {
    if ($leaseHeld) { $lease.ReleaseMutex() }
    if ($lease) { $lease.Dispose() }
}
if ($Log) { [IO.File]::WriteAllText($Log, $message + [char]10, $Utf8) }
Write-Output $message
exit $exitCode
