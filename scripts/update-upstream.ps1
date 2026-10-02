[CmdletBinding()]
param(
    [switch]$Push
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Push-Location $repoRoot
try {
    $inside = (& git rev-parse --is-inside-work-tree 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or $inside -ne 'true') {
        throw 'This script must run inside the IIB Git repository.'
    }

    $branch = (& git branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') {
        throw 'Upstream sync is allowed only from the main branch.'
    }

    $dirty = @(& git status --porcelain)
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to inspect the Git working tree.'
    }
    if ($dirty.Count -gt 0) {
        throw 'Working tree is not clean. Commit or discard local changes before syncing upstream.'
    }

    $origin = (& git remote get-url origin).Trim()
    $upstream = (& git remote get-url upstream).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $origin -or -not $upstream) {
        throw 'Both origin and upstream remotes are required.'
    }
    if ($upstream -ne 'https://github.com/zanllp/infinite-image-browsing.git') {
        throw 'The upstream remote does not point to the expected official repository.'
    }

    & git fetch upstream --prune
    if ($LASTEXITCODE -ne 0) {
        throw 'git fetch upstream failed.'
    }

    & git merge --no-edit upstream/main
    if ($LASTEXITCODE -ne 0) {
        throw 'Upstream merge stopped. Resolve the Git conflict before continuing.'
    }

    if ($Push) {
        & git push origin main
        if ($LASTEXITCODE -ne 0) {
            throw 'git push origin main failed.'
        }
    }

    Write-Host 'IIB upstream sync completed.'
    if (-not $Push) {
        Write-Host 'Review the merge, then run git push origin main (or rerun with -Push).'
    }
}
finally {
    Pop-Location
}
