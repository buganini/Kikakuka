[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

function Invoke-Git {
    param([string[]] $GitArguments)

    $output = & git @GitArguments
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArguments -join ' ') failed with exit code $LASTEXITCODE"
    }
    return $output
}

$repoRoot = (Invoke-Git @("rev-parse", "--show-toplevel") | Select-Object -Last 1)
if (-not $repoRoot) {
    throw "Could not determine the Git repository root."
}
$repoRoot = [System.IO.Path]::GetFullPath($repoRoot)

$links = @()
$indexLines = Invoke-Git @("-C", $repoRoot, "-c", "core.quotePath=false", "ls-files", "--stage")
foreach ($line in $indexLines) {
    if ($line -notmatch '^120000 [0-9a-f]+ [0-9]+\t(.+)$') {
        continue
    }

    $gitPath = $Matches[1]
    $targetLines = & git -C $repoRoot cat-file blob ":$gitPath"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not read the symlink target for '$gitPath'."
    }
    $target = (($targetLines -join "`n").TrimEnd([char[]] "`r`n"))
    if (-not $target) {
        throw "The symlink '$gitPath' has an empty target."
    }

    $destination = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $gitPath))
    $source = [System.IO.Path]::GetFullPath(
        (Join-Path (Split-Path -Parent $destination) $target)
    )
    if (-not (Test-Path -LiteralPath $source)) {
        throw "The target for '$gitPath' does not exist: $source"
    }

    $links += [PSCustomObject]@{
        GitPath = $gitPath
        Source = $source
        Destination = $destination
        IsDirectory = (Get-Item -LiteralPath $source -Force).PSIsContainer
    }
}

if ($links.Count -eq 0) {
    Write-Host "No tracked symlinks found."
    exit 0
}

# Materialize file links before directory links. A linked directory may contain
# tracked file links, and should receive their materialized contents.
$materialized = 0
foreach ($link in ($links | Sort-Object IsDirectory)) {
    if (Test-Path -LiteralPath $link.Destination) {
        $existing = Get-Item -LiteralPath $link.Destination -Force
        $isReparsePoint = (($existing.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)
        if ($existing.PSIsContainer -and -not $isReparsePoint) {
            Remove-Item -LiteralPath $link.Destination -Recurse -Force
        } else {
            Remove-Item -LiteralPath $link.Destination -Force
        }
    }

    Copy-Item -LiteralPath $link.Source -Destination $link.Destination -Recurse -Force
    Write-Host "Materialized $($link.GitPath)"
    $materialized++
}

Write-Host "Materialized $materialized tracked symlink(s)."
