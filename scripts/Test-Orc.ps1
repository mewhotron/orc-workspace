# Verify the installed instruction workspace; does not exercise provider accounts or native agents.
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$Destination)
$ErrorActionPreference = 'Stop'

$script:OrcImportOnly = $true
. (Join-Path $PSScriptRoot 'Install-Orc.ps1') -Destination $Destination
$script:OrcImportOnly = $false

$root = [IO.Path]::GetFullPath($Destination)
Assert-OrcNoReparse $root
$inventory = Read-OrcInventory $root
if (-not $inventory) { throw 'Orc Workspace install inventory is missing.' }
$missing = @()
$changed = @()
foreach ($file in @($inventory.files)) {
    $path = Get-OrcFullPath $root ([string]$file.path)
    Assert-OrcNoReparse $path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $missing += [string]$file.path; continue }
    if ((Get-OrcHash $path) -ne ([string]$file.sha256).ToLowerInvariant()) { $changed += [string]$file.path }
}
$ready = ($missing.Count -eq 0 -and $changed.Count -eq 0)
[pscustomobject]@{
    Status = $(if ($ready) { 'Ready' } else { 'NeedsAttention' })
    Destination = $root
    SourceVersion = [string]$inventory.source_version
    Adapter = [string]$inventory.adapter
    Packs = @($inventory.packs)
    FilesChecked = @($inventory.files).Count
    Missing = @($missing)
    Changed = @($changed)
    ExternalDependencies = 'Not verified: AI host, authentication, native agents, optional Python/FFmpeg and integrations require separate setup.'
}
if (-not $ready) { exit 1 }
