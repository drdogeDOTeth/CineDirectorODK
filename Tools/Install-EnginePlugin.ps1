<#
.SYNOPSIS
    Install CineDirector into the Otherside ODK engine, for every project at once.

.DESCRIPTION
    A plugin under <Engine>/Plugins is visible to every project that engine opens,
    so it only has to be installed once. This plugin is content-only Python with
    nothing to compile, so the install is just putting the folder in the right
    place.

    By default it creates a directory junction rather than a copy, so this
    checkout stays the single source of truth and `git pull` updates every project
    at once. Use -Copy if you would rather have an independent snapshot, for
    instance on a machine that does not have this repo.

    The ODK installs into numbered folders (Installs\1, Installs\2, ...), so an
    engine update lands somewhere new and takes the plugin with it. Re-run this
    script after an ODK update; with no arguments it finds the highest-numbered
    install by itself.

.PARAMETER EngineRoot
    The engine to install into, the folder that contains Engine\Plugins.
    Default: the highest-numbered install under C:\ODK\Installs.

.PARAMETER Copy
    Copy the files instead of linking to this checkout.

.PARAMETER Uninstall
    Remove the installed plugin and exit.

.EXAMPLE
    .\Tools\Install-EnginePlugin.ps1
.EXAMPLE
    .\Tools\Install-EnginePlugin.ps1 -EngineRoot "D:\ODK\Installs\2\output\Windows"
.EXAMPLE
    .\Tools\Install-EnginePlugin.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string] $EngineRoot,
    [switch] $Copy,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$PluginName = 'CineDirectorODK'
$SourceRoot = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path (Join-Path $SourceRoot "$PluginName.uplugin"))) {
    throw "$PluginName.uplugin is not in $SourceRoot. Run this script from inside the repo."
}

function Find-OdkEngine {
    $installs = 'C:\ODK\Installs'
    if (-not (Test-Path $installs)) {
        throw "No ODK install found under $installs. Pass -EngineRoot explicitly."
    }
    # Numbered installs, newest first. Sorted as numbers, so 10 beats 9.
    $candidates =
        Get-ChildItem -Path $installs -Directory |
        Sort-Object { [int]($_.Name -replace '\D', '0') } -Descending |
        ForEach-Object { Join-Path $_.FullName 'output\Windows' } |
        Where-Object { Test-Path (Join-Path $_ 'Engine\Plugins') }

    if (-not $candidates) {
        throw "No folder under $installs looks like an engine. Pass -EngineRoot explicitly."
    }
    return @($candidates)[0]
}

if (-not $EngineRoot) { $EngineRoot = Find-OdkEngine }

$PluginsRoot = Join-Path $EngineRoot 'Engine\Plugins\Marketplace'
if (-not (Test-Path (Join-Path $EngineRoot 'Engine\Plugins'))) {
    throw "$EngineRoot does not contain Engine\Plugins, so it is not an engine root."
}
$Target = Join-Path $PluginsRoot $PluginName

Write-Host "Engine:  $EngineRoot"
Write-Host "Target:  $Target"

# An existing install has to go first either way. Remove-Item on a junction
# deletes the link, not what it points at, but only when the junction is given
# directly rather than walked into, which is what -Force -Recurse does here.
if (Test-Path $Target) {
    $existing = Get-Item $Target -Force
    if ($existing.LinkType) {
        Write-Host "Removing existing link..."
        # .Delete() on a link never touches the target's contents.
        $existing.Delete()
    } else {
        Write-Host "Removing existing copy..."
        Remove-Item $Target -Recurse -Force
    }
}

if ($Uninstall) {
    Write-Host "Uninstalled. Restart the editor." -ForegroundColor Green
    exit 0
}

New-Item -ItemType Directory -Force -Path $PluginsRoot | Out-Null

if ($Copy) {
    Write-Host "Copying..."
    # __pycache__ is per-interpreter and .git has no business in an engine folder.
    robocopy $SourceRoot $Target /MIR /NFL /NDL /NJH /NJS /NP `
        /XD '__pycache__' '.git' 'Binaries' 'Intermediate' | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE." }
    $LASTEXITCODE = 0
    Write-Host "Copied." -ForegroundColor Green
} else {
    Write-Host "Linking..."
    # A junction, not a symlink: junctions need no elevation and no developer mode.
    New-Item -ItemType Junction -Path $Target -Value $SourceRoot | Out-Null
    Write-Host "Linked to $SourceRoot" -ForegroundColor Green
}

Write-Host ""
Write-Host "Done. Restart the editor; the menus appear under Tools > CineDirector."
Write-Host "If a project also has $PluginName in its own Plugins folder, remove it:"
Write-Host "two plugins with the same name is a startup error."
