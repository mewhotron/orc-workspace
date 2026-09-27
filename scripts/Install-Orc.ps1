# Orc Workspace installer. Windows PowerShell 5.1 and PowerShell 7 compatible.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$Destination,
    [AllowEmptyCollection()][ValidateSet('coaching', 'knowledge', 'research', 'media')][string[]]$Packs = @('coaching', 'knowledge', 'research', 'media'),
    [ValidateSet('generic', 'codex', 'claude', 'gemini')][string]$Adapter = 'generic'
)
$ErrorActionPreference = 'Stop'

function Assert-OrcRelativePath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path -match '[\\:<>"|?*]' -or $Path.StartsWith('/') -or
        $Path.EndsWith('/') -or $Path -match '(^|/)(\.|\.\.|)(/|$)' -or
        $Path -match '[\x00-\x1f]' -or $Path -match '(^|/)[^/]*[\. ](/|$)' -or
        $Path -match '(^|/)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.[^/]*)?(/|$)') {
        throw "Unsafe relative path in manifest or inventory: $Path"
    }
}

function Get-OrcFullPath {
    param([string]$Root, [string]$Relative)
    Assert-OrcRelativePath $Relative
    $path = [IO.Path]::GetFullPath((Join-Path $Root ($Relative.Replace('/', [IO.Path]::DirectorySeparatorChar))))
    $prefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw "Path escapes root: $Relative" }
    return $path
}

function Assert-OrcNoReparse {
    param([string]$Path)
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Reparse point in path: $cursor"
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor.TrimEnd('\', '/'))
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
}

function Get-OrcHash {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Read-OrcInventory {
    param([string]$Root)
    $path = Get-OrcFullPath $Root 'local/orc-install.json'
    Assert-OrcNoReparse $path
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    if ((Get-Item -LiteralPath $path).PSIsContainer) { throw 'Install inventory path is a directory.' }
    $inventory = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $inventory -or $inventory.schema_version -ne 1 -or $null -eq $inventory.files -or @($inventory.files).Count -eq 0) { throw 'Invalid install inventory.' }
    $seen = @{}
    foreach ($file in @($inventory.files)) {
        Assert-OrcRelativePath ([string]$file.path)
        if ([string]$file.path -eq 'local/orc-install.json' -or [string]$file.sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'Invalid install inventory entry.' }
        $key = ([string]$file.path).ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw 'Duplicate install inventory path.' }
        $seen[$key] = $true
    }
    return $inventory
}

function Get-OrcPackage {
    param([string]$SourceRoot)
    $manifestPath = Join-Path $SourceRoot 'release-manifest.json'
    Assert-OrcNoReparse $manifestPath
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'release-manifest.json is missing.' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $manifest -or $manifest.schema_version -ne 1 -or [string]::IsNullOrWhiteSpace([string]$manifest.version) -or $null -eq $manifest.files -or @($manifest.files).Count -eq 0) {
        throw 'Invalid release manifest.'
    }
    $seen = @{}
    foreach ($file in @($manifest.files)) {
        $relative = [string]$file.path
        Assert-OrcRelativePath $relative
        if ($relative -eq 'release-manifest.json' -or $relative -eq 'local/orc-install.json' -or
            [string]$file.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or
            [string]$file.pack -notin @('core', 'coaching', 'knowledge', 'research', 'media')) { throw "Invalid release entry: $relative" }
        $key = $relative.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "Duplicate release path: $relative" }
        $seen[$key] = $true
        $source = Get-OrcFullPath $SourceRoot $relative
        Assert-OrcNoReparse $source
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "Missing package file: $relative" }
        if ((Get-OrcHash $source) -ne ([string]$file.sha256).ToLowerInvariant()) { throw "Package hash mismatch: $relative" }
    }
    return $manifest
}

function Assert-OrcSeparateRoots {
    param([string]$SourceRoot, [string]$TargetRoot)
    $source = [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\', '/')
    $target = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\', '/')
    if ($source.Equals($target, [StringComparison]::OrdinalIgnoreCase) -or
        $source.StartsWith($target + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase) -or
        $target.StartsWith($source + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Package source and destination overlap.'
    }
}

function New-OrcSelection {
    param($Manifest, [string[]]$SelectedPacks, [string]$SelectedAdapter)
    $owned = @()
    $localTemplates = @()
    foreach ($file in @($Manifest.files)) {
        $path = [string]$file.path
        $pack = [string]$file.pack
        if ($pack -ne 'core' -and $pack -notin $SelectedPacks) { continue }
        if ($path -match '^templates/local/(.+\.md)$') {
            $localTemplates += [pscustomobject]@{ path = ('local/' + $Matches[1]); source = $path; sha256 = ([string]$file.sha256).ToLowerInvariant() }
        }
        $owned += [pscustomobject]@{ path = $path; source = $path; sha256 = ([string]$file.sha256).ToLowerInvariant() }
        if ($path -match '^adapters/([^/]+)/overlay/(.+)$' -and $Matches[1] -eq $SelectedAdapter) {
            $owned += [pscustomobject]@{ path = $Matches[2]; source = $path; sha256 = ([string]$file.sha256).ToLowerInvariant() }
        }
    }
    $seen = @{}
    foreach ($file in $owned) {
        Assert-OrcRelativePath $file.path
        if ($file.path -eq 'local/orc-install.json') { throw 'Package overlay targets reserved install inventory.' }
        $key = $file.path.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "Multiple package files map to $($file.path)" }
        $seen[$key] = $true
    }
    foreach ($file in $localTemplates) {
        if ($seen.ContainsKey($file.path.ToLowerInvariant())) { throw "Template conflicts with owned file: $($file.path)" }
    }
    return [pscustomobject]@{ files = @($owned); templates = @($localTemplates) }
}

function Invoke-OrcInstall {
    param([string]$DestinationPath, [string[]]$SelectedPacks, [string]$SelectedAdapter, [bool]$Preview)
    if ([string]::IsNullOrWhiteSpace($DestinationPath)) { throw 'Specify -Destination.' }
    $sourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    $targetRoot = [IO.Path]::GetFullPath($DestinationPath)
    if ($targetRoot.StartsWith('\\', [StringComparison]::Ordinal)) { throw 'Network destinations are not supported.' }
    Assert-OrcSeparateRoots $sourceRoot $targetRoot
    Assert-OrcNoReparse $sourceRoot
    Assert-OrcNoReparse $targetRoot
    if (Test-Path -LiteralPath $targetRoot -PathType Leaf) { throw 'Destination is a file.' }
    $manifest = Get-OrcPackage $sourceRoot
    $selection = New-OrcSelection $manifest $SelectedPacks $SelectedAdapter
    $prior = Read-OrcInventory $targetRoot
    $priorInventoryPath = Get-OrcFullPath $targetRoot 'local/orc-install.json'
    $priorInventoryHash = if (Test-Path -LiteralPath $priorInventoryPath -PathType Leaf) { Get-OrcHash $priorInventoryPath } else { $null }
    $priorByPath = @{}
    if ($prior) { foreach ($item in @($prior.files)) { $priorByPath[([string]$item.path).ToLowerInvariant()] = $item } }
    $nextByPath = @{}
    foreach ($item in $selection.files) { $nextByPath[$item.path.ToLowerInvariant()] = $item }

    # Complete conflict and path preflight before creating the destination or stage.
    foreach ($item in @($selection.files) + @($prior.files)) {
        if ($null -eq $item) { continue }
        $path = Get-OrcFullPath $targetRoot ([string]$item.path)
        Assert-OrcNoReparse $path
        $parent = [IO.Path]::GetDirectoryName($path)
        while ($parent -and $parent.StartsWith($targetRoot, [StringComparison]::OrdinalIgnoreCase)) {
            if (Test-Path -LiteralPath $parent -PathType Leaf) { throw "A parent path is a file: $parent" }
            if ($parent -eq $targetRoot) { break }
            $parent = [IO.Path]::GetDirectoryName($parent)
        }
        if (Test-Path -LiteralPath $path) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Destination path is not a file: $path" }
            $key = ([string]$item.path).ToLowerInvariant()
            if (-not $priorByPath.ContainsKey($key)) { throw "Existing unowned file: $path" }
            if ((Get-OrcHash $path) -ne ([string]$priorByPath[$key].sha256).ToLowerInvariant()) { throw "User-modified owned file: $path" }
        } elseif ($priorByPath.ContainsKey(([string]$item.path).ToLowerInvariant())) {
            throw "Previously owned file is missing: $path"
        }
    }
    foreach ($item in $selection.templates) {
        $path = Get-OrcFullPath $targetRoot $item.path
        Assert-OrcNoReparse $path
        if ((Test-Path -LiteralPath $path) -and -not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Local template target is not a file: $path" }
        $parent = [IO.Path]::GetDirectoryName($path)
        if (Test-Path -LiteralPath $parent -PathType Leaf) { throw "Local template parent is a file: $parent" }
    }

    $changes = @()
    foreach ($item in $selection.files) {
        $path = Get-OrcFullPath $targetRoot $item.path
        if (-not (Test-Path -LiteralPath $path) -or (Get-OrcHash $path) -ne $item.sha256) { $changes += $item.path }
    }
    foreach ($key in $priorByPath.Keys) { if (-not $nextByPath.ContainsKey($key)) { $changes += [string]$priorByPath[$key].path } }
    foreach ($item in $selection.templates) {
        $path = Get-OrcFullPath $targetRoot $item.path
        if (-not (Test-Path -LiteralPath $path)) { $changes += $item.path }
    }
    $inventoryPath = Get-OrcFullPath $targetRoot 'local/orc-install.json'
    $newInventory = [ordered]@{
        schema_version = 1; source_version = [string]$manifest.version; adapter = $SelectedAdapter
        packs = @($SelectedPacks); files = @($selection.files | ForEach-Object { [ordered]@{ path = $_.path; sha256 = $_.sha256 } })
    }
    $inventoryJson = $newInventory | ConvertTo-Json -Depth 8
    $inventoryChanged = -not (Test-Path -LiteralPath $inventoryPath) -or ((Get-Content -LiteralPath $inventoryPath -Raw -Encoding UTF8).Trim() -ne $inventoryJson.Trim())
    if ($Preview) {
        return [pscustomobject]@{ Status = 'WhatIf'; Destination = $targetRoot; Version = $manifest.version; Adapter = $SelectedAdapter; Packs = @($SelectedPacks); Changes = @($changes); InventoryChange = $inventoryChanged }
    }
    if ($changes.Count -eq 0 -and -not $inventoryChanged) {
        return [pscustomobject]@{ Status = 'Ready'; Destination = $targetRoot; Version = $manifest.version; Adapter = $SelectedAdapter; Packs = @($SelectedPacks); Changes = @() }
    }
    if (-not $PSCmdlet.ShouldProcess($targetRoot, 'Install Orc Workspace files')) { return }

    $stage = Join-Path $targetRoot ('.orc-stage-' + [guid]::NewGuid().ToString('N'))
    $changedPaths = New-Object 'System.Collections.Generic.List[string]'
    $backedUp = @{}
    $preserveStage = $false
    try {
        New-Item -ItemType Directory -Path $targetRoot -Force | Out-Null
        New-Item -ItemType Directory -Path $stage | Out-Null
        # Back up every pre-existing file touched, then stage verified source bytes.
        foreach ($relative in @($changes)) {
            $target = Get-OrcFullPath $targetRoot $relative
            Assert-OrcNoReparse $target
            if (Test-Path -LiteralPath $target -PathType Leaf) {
                $backup = Join-Path $stage ('backup-' + $backedUp.Count)
                Copy-Item -LiteralPath $target -Destination $backup
                $backedUp[$relative.ToLowerInvariant()] = $backup
            }
        }
        if (Test-Path -LiteralPath $inventoryPath -PathType Leaf) {
            Copy-Item -LiteralPath $inventoryPath -Destination (Join-Path $stage 'inventory-backup')
        }
        foreach ($item in $selection.files) {
            if ($item.path -notin $changes) { continue }
            $source = Get-OrcFullPath $sourceRoot $item.source
            $target = Get-OrcFullPath $targetRoot $item.path
            Assert-OrcNoReparse $source; Assert-OrcNoReparse $target
            if ((Get-OrcHash $source) -ne $item.sha256) { throw "Package changed during installation: $($item.source)" }
            $parent = [IO.Path]::GetDirectoryName($target)
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
            $staged = Join-Path $stage ('new-' + $changedPaths.Count)
            Copy-Item -LiteralPath $source -Destination $staged
            if ((Get-OrcHash $staged) -ne $item.sha256) { throw "Staging hash mismatch: $($item.source)" }
            $key = $item.path.ToLowerInvariant()
            if (Test-Path -LiteralPath $target) {
                if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or -not $priorByPath.ContainsKey($key) -or
                    (Get-OrcHash $target) -ne ([string]$priorByPath[$key].sha256).ToLowerInvariant()) {
                    throw "Destination changed since preflight: $($item.path)"
                }
                Remove-Item -LiteralPath $target -Force
            } elseif ($priorByPath.ContainsKey($key)) { throw "Owned file disappeared since preflight: $($item.path)" }
            $changedPaths.Add($item.path)
            Move-Item -LiteralPath $staged -Destination $target
            if (-not (Test-Path -LiteralPath $target -PathType Leaf) -or (Get-OrcHash $target) -ne $item.sha256) { throw "Install move did not complete: $($item.path)" }
        }
        foreach ($key in $priorByPath.Keys) {
            if ($nextByPath.ContainsKey($key)) { continue }
            $relative = [string]$priorByPath[$key].path
            $target = Get-OrcFullPath $targetRoot $relative
            Assert-OrcNoReparse $target
            if ((Get-OrcHash $target) -ne ([string]$priorByPath[$key].sha256).ToLowerInvariant()) { throw "Owned file changed during installation: $relative" }
            Remove-Item -LiteralPath $target -Force
            $changedPaths.Add($relative)
            if (Test-Path -LiteralPath $target) { throw "Owned file removal did not complete: $relative" }
        }
        foreach ($item in $selection.templates) {
            $target = Get-OrcFullPath $targetRoot $item.path
            Assert-OrcNoReparse $target
            if (Test-Path -LiteralPath $target) { continue }
            $source = Get-OrcFullPath $sourceRoot $item.source
            if ((Get-OrcHash $source) -ne $item.sha256) { throw "Template changed during installation: $($item.source)" }
            New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force | Out-Null
            $changedPaths.Add($item.path)
            Copy-Item -LiteralPath $source -Destination $target
            if ((Get-OrcHash $target) -ne $item.sha256) { throw "Local template copy did not complete: $($item.path)" }
        }
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($inventoryPath)) -Force | Out-Null
        $inventoryTemp = Join-Path $stage 'inventory-new'
        [IO.File]::WriteAllText($inventoryTemp, $inventoryJson + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $inventoryPath) {
            if ($null -eq $priorInventoryHash -or (Get-OrcHash $inventoryPath) -ne $priorInventoryHash) { throw 'Install inventory changed since preflight.' }
            Remove-Item -LiteralPath $inventoryPath -Force
        } elseif ($null -ne $priorInventoryHash) { throw 'Install inventory disappeared since preflight.' }
        Move-Item -LiteralPath $inventoryTemp -Destination $inventoryPath
    } catch {
        $originalFailure = $_
        $rollbackErrors = New-Object 'System.Collections.Generic.List[string]'
        foreach ($relative in $changedPaths) {
            try {
                $target = Get-OrcFullPath $targetRoot $relative
                if (Test-Path -LiteralPath $target -PathType Leaf) {
                    $key = $relative.ToLowerInvariant()
                    $expected = if ($nextByPath.ContainsKey($key)) { [string]$nextByPath[$key].sha256 } else {
                        $template = @($selection.templates | Where-Object { $_.path -eq $relative } | Select-Object -First 1)
                        if ($template.Count) { [string]$template[0].sha256 } else { $null }
                    }
                    if ($null -ne $expected -and (Get-OrcHash $target) -ne $expected) { throw "Changed during rollback: $relative" }
                    Remove-Item -LiteralPath $target -Force
                }
            } catch { $rollbackErrors.Add([string]$_.Exception.Message) }
        }
        foreach ($key in $backedUp.Keys) {
            try {
                $target = Get-OrcFullPath $targetRoot $key
                if ((Test-Path -LiteralPath $target) -and (Get-OrcHash $target) -ne (Get-OrcHash $backedUp[$key])) { throw "Restore target occupied: $key" }
                New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force | Out-Null
                Copy-Item -LiteralPath $backedUp[$key] -Destination $target -Force
            } catch { $rollbackErrors.Add([string]$_.Exception.Message) }
        }
        try {
            if (Test-Path -LiteralPath (Join-Path $stage 'inventory-backup')) {
                Copy-Item -LiteralPath (Join-Path $stage 'inventory-backup') -Destination $inventoryPath -Force
            } elseif (Test-Path -LiteralPath $inventoryPath) { Remove-Item -LiteralPath $inventoryPath -Force }
        } catch { $rollbackErrors.Add([string]$_.Exception.Message) }
        if ($rollbackErrors.Count -gt 0) {
            $preserveStage = $true
            throw "Installation failed: $($originalFailure.Exception.Message). Rollback incomplete: $($rollbackErrors -join '; '). Backups retained at $stage"
        }
        throw $originalFailure
    } finally {
        $stageFull = [IO.Path]::GetFullPath($stage)
        if (-not $stageFull.StartsWith($targetRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe staging directory.' }
        if (-not $preserveStage -and (Test-Path -LiteralPath $stageFull)) {
            Assert-OrcNoReparse $stageFull
            try { Remove-Item -LiteralPath $stageFull -Recurse -Force }
            catch { throw "Staging cleanup failed; inspect $stageFull. $($_.Exception.Message)" }
        }
    }
    return [pscustomobject]@{ Status = 'Ready'; Destination = $targetRoot; Version = $manifest.version; Adapter = $SelectedAdapter; Packs = @($SelectedPacks); Changes = @($changes) }
}

if (-not $script:OrcImportOnly) {
    Invoke-OrcInstall -DestinationPath $Destination -SelectedPacks $Packs -SelectedAdapter $Adapter -Preview ([bool]$WhatIfPreference)
}
