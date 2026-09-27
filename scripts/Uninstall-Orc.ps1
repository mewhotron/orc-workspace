# Remove only unchanged installer-owned files. Starter local records remain private user data.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param([Parameter(Mandatory = $true)][string]$Destination)
$ErrorActionPreference = 'Stop'

$script:OrcImportOnly = $true
. (Join-Path $PSScriptRoot 'Install-Orc.ps1') -Destination $Destination
$script:OrcImportOnly = $false

$root = [IO.Path]::GetFullPath($Destination)
if ($root.StartsWith('\\', [StringComparison]::Ordinal)) { throw 'Network destinations are not supported.' }
Assert-OrcNoReparse $root
$inventory = Read-OrcInventory $root
if (-not $inventory) { throw 'Orc Workspace install inventory is missing.' }
$inventoryPath = Get-OrcFullPath $root 'local/orc-install.json'
$targets = @()
foreach ($file in @($inventory.files)) {
    $path = Get-OrcFullPath $root ([string]$file.path)
    Assert-OrcNoReparse $path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Owned file is missing: $($file.path)" }
    if ((Get-OrcHash $path) -ne ([string]$file.sha256).ToLowerInvariant()) { throw "User-modified owned file: $($file.path)" }
    $targets += [pscustomobject]@{ path = [string]$file.path; full = $path; sha256 = ([string]$file.sha256).ToLowerInvariant() }
}
if ($WhatIfPreference) {
    [pscustomobject]@{ Status = 'WhatIf'; Destination = $root; FilesToRemove = @($targets | ForEach-Object { $_.path }); Preserved = 'local/ user records and unowned files' }
    return
}
if (-not $PSCmdlet.ShouldProcess($root, 'Remove unchanged Orc Workspace files')) { return }
$stage = Join-Path $root ('.orc-uninstall-' + [guid]::NewGuid().ToString('N'))
$removed = New-Object 'System.Collections.Generic.List[string]'
$preserveStage = $false
try {
    New-Item -ItemType Directory -Path $stage | Out-Null
    $i = 0
    foreach ($target in $targets) {
        Assert-OrcNoReparse $target.full
        if ((Get-OrcHash $target.full) -ne ([string](@($inventory.files) | Where-Object { $_.path -eq $target.path } | Select-Object -First 1).sha256).ToLowerInvariant()) {
            throw "Owned file changed during uninstall: $($target.path)"
        }
        $removed.Add($target.path)
        $backup = Join-Path $stage ('file-' + $i)
        Move-Item -LiteralPath $target.full -Destination $backup
        if ((Test-Path -LiteralPath $target.full) -or -not (Test-Path -LiteralPath $backup -PathType Leaf)) { throw "Uninstall move did not complete: $($target.path)" }
        $i++
    }
    Move-Item -LiteralPath $inventoryPath -Destination (Join-Path $stage 'inventory')
    if ((Test-Path -LiteralPath $inventoryPath) -or -not (Test-Path -LiteralPath (Join-Path $stage 'inventory') -PathType Leaf)) { throw 'Install inventory move did not complete.' }
} catch {
    $originalFailure = $_
    $rollbackErrors = New-Object 'System.Collections.Generic.List[string]'
    $i = 0
    foreach ($target in $targets) {
        if ($target.path -in $removed) {
            try {
                $backup = Join-Path $stage ('file-' + $i)
                if (-not (Test-Path -LiteralPath $backup -PathType Leaf)) {
                    if (-not (Test-Path -LiteralPath $target.full -PathType Leaf) -or
                        (Get-OrcHash $target.full) -ne $target.sha256) { throw "Missing uninstall backup: $($target.path)" }
                } else {
                    if (Test-Path -LiteralPath $target.full) { throw "Restore target occupied: $($target.path)" }
                    Move-Item -LiteralPath $backup -Destination $target.full
                }
            } catch { $rollbackErrors.Add([string]$_.Exception.Message) }
        }
        $i++
    }
    $backupInventory = Join-Path $stage 'inventory'
    try {
        if (Test-Path -LiteralPath $backupInventory) {
            if (Test-Path -LiteralPath $inventoryPath) { throw 'Install inventory restore target occupied.' }
            Move-Item -LiteralPath $backupInventory -Destination $inventoryPath
        }
    } catch { $rollbackErrors.Add([string]$_.Exception.Message) }
    if ($rollbackErrors.Count -gt 0) {
        $preserveStage = $true
        throw "Uninstall failed: $($originalFailure.Exception.Message). Rollback incomplete: $($rollbackErrors -join '; '). Backups retained at $stage"
    }
    throw $originalFailure
} finally {
    $stageFull = [IO.Path]::GetFullPath($stage)
    if (-not $stageFull.StartsWith($root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe staging directory.' }
    if (-not $preserveStage -and (Test-Path -LiteralPath $stageFull)) {
        Assert-OrcNoReparse $stageFull
        try { Remove-Item -LiteralPath $stageFull -Recurse -Force }
        catch { throw "Staging cleanup failed; inspect $stageFull. $($_.Exception.Message)" }
    }
}
[pscustomobject]@{ Status = 'Removed'; Destination = $root; Removed = @($removed); Preserved = 'local/ user records and unowned files' }
