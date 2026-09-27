# Synthetic, local-only installer checks. Does not read private project data.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$sourceScripts = Join-Path $PSScriptRoot '..\scripts'
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('orc-installer-test-' + [guid]::NewGuid().ToString('N'))
$package = Join-Path $scratch 'package with spaces'
$install = Join-Path $scratch 'install with spaces'

function Assert-True { param([bool]$Condition, [string]$Message) if (-not $Condition) { throw $Message } }
function Expect-Failure { param([scriptblock]$Action, [string]$Message) $failed = $false; try { & $Action } catch { $failed = $true }; Assert-True $failed $Message }
function Put-File { param([string]$Relative, [string]$Content)
    $path = Join-Path $package ($Relative.Replace('/', '\'))
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($path)) -Force | Out-Null
    [IO.File]::WriteAllText($path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}
function Write-Manifest {
    param([string[]]$RelativePaths)
    $entries = foreach ($relative in $RelativePaths) {
        $pack = if ($relative -match '^packs/([^/]+)/') { $Matches[1] } else { 'core' }
        [ordered]@{ path = $relative; sha256 = (Get-FileHash -LiteralPath (Join-Path $package ($relative.Replace('/', '\'))) -Algorithm SHA256).Hash.ToLowerInvariant(); pack = $pack }
    }
    $manifest = [ordered]@{ schema_version = 1; version = '0.1.0'; files = @($entries) } | ConvertTo-Json -Depth 6
    [IO.File]::WriteAllText((Join-Path $package 'release-manifest.json'), $manifest, (New-Object System.Text.UTF8Encoding($false)))
}

try {
    New-Item -ItemType Directory -Path (Join-Path $package 'scripts') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $sourceScripts 'Install-Orc.ps1') -Destination (Join-Path $package 'scripts\Install-Orc.ps1')
    Copy-Item -LiteralPath (Join-Path $sourceScripts 'Test-Orc.ps1') -Destination (Join-Path $package 'scripts\Test-Orc.ps1')
    Copy-Item -LiteralPath (Join-Path $sourceScripts 'Uninstall-Orc.ps1') -Destination (Join-Path $package 'scripts\Uninstall-Orc.ps1')
    Put-File 'core/ORC.md' 'core-v1'
    Put-File 'packs/coaching/COACH.md' 'coach-v1'
    Put-File 'packs/media/MEDIA.md' 'media-v1'
    Put-File 'templates/local/preferences.md' 'starter'
    Put-File 'adapters/claude/overlay/CLAUDE.md' 'claude-entry'
    Put-File 'adapters/gemini/overlay/GEMINI.md' 'gemini-entry'
    $paths = @('scripts/Install-Orc.ps1','scripts/Test-Orc.ps1','scripts/Uninstall-Orc.ps1','core/ORC.md','packs/coaching/COACH.md','packs/media/MEDIA.md','templates/local/preferences.md','adapters/claude/overlay/CLAUDE.md','adapters/gemini/overlay/GEMINI.md')
    Write-Manifest $paths
    $installer = Join-Path $package 'scripts\Install-Orc.ps1'
    $tester = Join-Path $package 'scripts\Test-Orc.ps1'
    $uninstaller = Join-Path $package 'scripts\Uninstall-Orc.ps1'

    $preview = & $installer -Destination $install -Packs @('coaching') -Adapter claude -WhatIf
    Assert-True ($preview.Status -eq 'WhatIf' -and -not (Test-Path -LiteralPath $install)) 'WhatIf wrote files.'
    & $installer -Destination $install -Packs @('coaching') -Adapter claude | Out-Null
    Assert-True (Test-Path -LiteralPath (Join-Path $install 'CLAUDE.md')) 'Chosen adapter overlay absent.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $install 'GEMINI.md'))) 'Unchosen overlay installed.'
    Assert-True (Test-Path -LiteralPath (Join-Path $install 'packs\coaching\COACH.md')) 'Selected pack absent.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $install 'packs\media\MEDIA.md'))) 'Unselected pack installed.'
    Assert-True ((& $tester -Destination $install).Status -eq 'Ready') 'Clean install did not verify.'
    Assert-True ((& $installer -Destination $install -Packs @('coaching') -Adapter claude).Changes.Count -eq 0) 'Repeat install changed files.'

    [IO.File]::WriteAllText((Join-Path $install 'local\preferences.md'), 'private customization')
    & $installer -Destination $install -Packs @('coaching') -Adapter claude | Out-Null
    Assert-True ((Get-Content -LiteralPath (Join-Path $install 'local\preferences.md') -Raw) -eq 'private customization') 'Local customization overwritten.'
    Put-File 'core/ORC.md' 'core-v2'
    Write-Manifest $paths
    & $installer -Destination $install -Packs @('coaching') -Adapter claude | Out-Null
    Assert-True ((Get-Content -LiteralPath (Join-Path $install 'core\ORC.md') -Raw) -eq 'core-v2') 'Unmodified owned file did not update.'

    Put-File 'packs/media/MEDIA.md' 'media-v2'
    Write-Manifest $paths
    & $installer -Destination $install -Packs @() -Adapter generic | Out-Null
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $install 'CLAUDE.md'))) 'Stale overlay was not removed.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $install 'packs\coaching\COACH.md'))) 'Deselected pack was not removed.'
    Assert-True ((& $tester -Destination $install).Status -eq 'Ready') 'Core-only install did not verify.'

    $foreign = Join-Path $install 'packs\media\MEDIA.md'
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($foreign)) -Force | Out-Null
    [IO.File]::WriteAllText($foreign, 'foreign')
    Expect-Failure { & $installer -Destination $install -Packs @('media') -Adapter generic | Out-Null } 'Unowned file conflict was accepted.'
    Assert-True ((Get-Content -LiteralPath $foreign -Raw) -eq 'foreign') 'Unowned file changed.'
    Remove-Item -LiteralPath $foreign
    & $installer -Destination $install -Packs @('media') -Adapter generic | Out-Null
    [IO.File]::WriteAllText((Join-Path $install 'core\ORC.md'), 'edited-by-user')
    Expect-Failure { & $installer -Destination $install -Packs @('media') -Adapter generic | Out-Null } 'Modified owned file accepted by installer.'
    Expect-Failure { & $uninstaller -Destination $install | Out-Null } 'Modified owned file accepted by uninstaller.'
    Assert-True ((& $tester -Destination $install).Status -eq 'NeedsAttention') 'Verifier missed changed file.'
    [IO.File]::WriteAllText((Join-Path $install 'core\ORC.md'), 'core-v2')

    # A cmdlet that emits a nonterminating error must still stop installation and roll back.
    $inventoryBeforeFailure = Get-Content -LiteralPath (Join-Path $install 'local\orc-install.json') -Raw
    Put-File 'core/ORC.md' 'core-v3'
    Put-File 'packs/media/MEDIA.md' 'media-v3'
    Write-Manifest $paths
    $global:OrcFailMoveTo = Join-Path $install 'packs\media\MEDIA.md'
    function global:Move-Item {
        param([string]$LiteralPath, [string]$Destination, [switch]$Force)
        if ($Destination -eq $global:OrcFailMoveTo) { Write-Error 'Injected nonterminating Move-Item failure.'; return }
        Microsoft.PowerShell.Management\Move-Item @PSBoundParameters
    }
    $ErrorActionPreference = 'Continue'
    try {
        Expect-Failure { & $installer -Destination $install -Packs @('media') -Adapter generic | Out-Null } 'Nonterminating move error was treated as success.'
    } finally {
        Remove-Item -LiteralPath Function:\Move-Item
        Remove-Variable -Name OrcFailMoveTo -Scope Global
        $ErrorActionPreference = 'Stop'
    }
    Assert-True ((Get-Content -LiteralPath (Join-Path $install 'core\ORC.md') -Raw) -eq 'core-v2') 'Failed update did not restore core.'
    Assert-True ((Get-Content -LiteralPath (Join-Path $install 'packs\media\MEDIA.md') -Raw) -eq 'media-v2') 'Failed update altered media.'
    Assert-True ((Get-Content -LiteralPath (Join-Path $install 'local\orc-install.json') -Raw) -eq $inventoryBeforeFailure) 'Failed update altered inventory.'
    Assert-True (@(Get-ChildItem -LiteralPath $install -Directory -Filter '.orc-stage-*').Count -eq 0) 'Successful rollback left staging data.'

    # If restoration itself fails, the error must name and retain the recovery backup.
    $brokenInstall = Join-Path $scratch 'recovery test'
    Put-File 'core/ORC.md' 'core-v2'
    Put-File 'packs/media/MEDIA.md' 'media-v2'
    Write-Manifest $paths
    & $installer -Destination $brokenInstall -Packs @('media') -Adapter generic | Out-Null
    Put-File 'core/ORC.md' 'core-v3'
    Put-File 'packs/media/MEDIA.md' 'media-v3'
    Write-Manifest $paths
    $global:OrcFailMoveTo = Join-Path $brokenInstall 'packs\media\MEDIA.md'
    $global:OrcFailRestoreTo = Join-Path $brokenInstall 'core\ORC.md'
    function global:Move-Item {
        param([string]$LiteralPath, [string]$Destination, [switch]$Force)
        if ($Destination -eq $global:OrcFailMoveTo) { Write-Error 'Injected move failure.'; return }
        Microsoft.PowerShell.Management\Move-Item @PSBoundParameters
    }
    function global:Copy-Item {
        param([string]$LiteralPath, [string]$Destination, [switch]$Force)
        if ($Destination -eq $global:OrcFailRestoreTo -and $LiteralPath -match 'backup-') { Write-Error 'Injected restore failure.'; return }
        Microsoft.PowerShell.Management\Copy-Item @PSBoundParameters
    }
    $ErrorActionPreference = 'Continue'
    $recoveryMessage = ''
    try {
        try { & $installer -Destination $brokenInstall -Packs @('media') -Adapter generic | Out-Null }
        catch { $recoveryMessage = $_.Exception.Message }
    } finally {
        Remove-Item -LiteralPath Function:\Move-Item
        Remove-Item -LiteralPath Function:\Copy-Item
        Remove-Variable -Name OrcFailMoveTo -Scope Global
        Remove-Variable -Name OrcFailRestoreTo -Scope Global
        $ErrorActionPreference = 'Stop'
    }
    $retained = @(Get-ChildItem -LiteralPath $brokenInstall -Directory -Filter '.orc-stage-*')
    Assert-True ($recoveryMessage -match 'Rollback incomplete' -and $retained.Count -eq 1 -and $recoveryMessage.Contains($retained[0].FullName)) 'Incomplete rollback did not retain and report backup path.'
    Assert-True (@(Get-ChildItem -LiteralPath $retained[0].FullName -File -Filter 'backup-*').Count -gt 0) 'Retained staging has no recovery backup.'

    $manifestPath = Join-Path $package 'release-manifest.json'
    $originalManifest = Get-Content -LiteralPath $manifestPath -Raw
    $bad = $originalManifest | ConvertFrom-Json
    $bad.files[0].path = '../escape.txt'
    [IO.File]::WriteAllText($manifestPath, ($bad | ConvertTo-Json -Depth 6))
    Expect-Failure { & $installer -Destination (Join-Path $scratch 'bad target') -Packs @() | Out-Null } 'Traversal manifest accepted.'
    [IO.File]::WriteAllText($manifestPath, $originalManifest)
    Expect-Failure { & $installer -Destination (Join-Path $package 'inside') -Packs @() | Out-Null } 'Source/destination overlap accepted.'

    if (Test-Path -LiteralPath (Join-Path $scratch 'junction')) { throw 'Unexpected junction path.' }
    try {
        New-Item -ItemType Junction -Path (Join-Path $scratch 'junction') -Target $install -ErrorAction Stop | Out-Null
        Expect-Failure { & $installer -Destination (Join-Path $scratch 'junction') -Packs @() | Out-Null } 'Reparse destination accepted.'
    } catch [System.Management.Automation.ParameterBindingException] { }
    $removePreview = & $uninstaller -Destination $install -WhatIf
    Assert-True ($removePreview.Status -eq 'WhatIf' -and (Test-Path -LiteralPath (Join-Path $install 'core\ORC.md'))) 'Uninstall WhatIf changed files.'
    $global:OrcFailMoveFrom = Join-Path $install 'core\ORC.md'
    function global:Move-Item {
        param([string]$LiteralPath, [string]$Destination, [switch]$Force)
        if ($LiteralPath -eq $global:OrcFailMoveFrom) { Write-Error 'Injected nonterminating uninstall move failure.'; return }
        Microsoft.PowerShell.Management\Move-Item @PSBoundParameters
    }
    $ErrorActionPreference = 'Continue'
    try {
        Expect-Failure { & $uninstaller -Destination $install | Out-Null } 'Nonterminating uninstall error was treated as success.'
    } finally {
        Remove-Item -LiteralPath Function:\Move-Item
        Remove-Variable -Name OrcFailMoveFrom -Scope Global
        $ErrorActionPreference = 'Stop'
    }
    Assert-True ((& $tester -Destination $install).Status -eq 'Ready') 'Failed uninstall did not restore install.'
    Assert-True (@(Get-ChildItem -LiteralPath $install -Directory -Filter '.orc-uninstall-*').Count -eq 0) 'Successful uninstall rollback left staging data.'
    & $uninstaller -Destination $install | Out-Null
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $install 'core\ORC.md'))) 'Owned file survived uninstall.'
    Assert-True ((Get-Content -LiteralPath (Join-Path $install 'local\preferences.md') -Raw) -eq 'private customization') 'Uninstall removed local data.'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $install 'local\orc-install.json'))) 'Inventory survived uninstall.'
    'PASS: install, repeat, update, packs, overlay, local records, conflicts, traversal, reparse (if available), WhatIf, injected IO failures, rollback and uninstall.'
} finally {
    $cleanupPath = [IO.Path]::GetFullPath($scratch)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if (-not $cleanupPath.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($cleanupPath) -notmatch '^orc-installer-test-[a-f0-9]{32}$') {
        throw 'Refusing cleanup outside the exact synthetic test directory.'
    }
    if (Test-Path -LiteralPath $cleanupPath) { Remove-Item -LiteralPath $cleanupPath -Recurse -Force }
}
