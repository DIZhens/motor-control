#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateNotNullOrEmpty()]
    [string]$RepositoryPath = $PSScriptRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $OutputEncoding

$directory = Get-Item -LiteralPath $RepositoryPath -Force
if ($directory.PSProvider.Name -ne 'FileSystem' -or -not $directory.PSIsContainer) {
    throw 'RepositoryPath must be a filesystem directory.'
}

# 禁止可选索引刷新、文件监视器和按需获取远端对象，只读取本地 Git。
$gitOptions = @(
    '--no-pager', '--no-optional-locks',
    '-c', 'core.quotepath=false', '-c', 'core.fsmonitor=false',
    '-c', 'i18n.logOutputEncoding=utf-8', '-c', 'protocol.allow=never',
    '-C', $directory.FullName
)
$previousLazyFetch = $env:GIT_NO_LAZY_FETCH
try {
    $env:GIT_NO_LAZY_FETCH = '1'
    $repositoryRoot = & git @gitOptions rev-parse --show-toplevel
    if ($LASTEXITCODE -ne 0) { throw 'RepositoryPath is not inside a local Git repository.' }
    $gitOptions[-1] = $repositoryRoot

    Write-Output "Repository: $repositoryRoot"
    Write-Output 'Branch (empty means detached HEAD):'
    & git @gitOptions branch --show-current
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the current branch.' }

    Write-Output "`nWorking tree (empty means clean):"
    & git @gitOptions status --short --ignore-submodules=all
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the working tree status.' }

    Write-Output "`nLast 5 local commits:"
    & git @gitOptions log -5 --format=fuller --no-decorate --no-show-signature
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read the local commit history.' }

    Write-Output "`nDocumentation entries (read as needed):"
    @(
        'README.md', 'DEVELOPMENT.md', 'ARCHITECTURE.md',
        'firmware/README.md', 'firmware/control/README.md', 'firmware/motor/README.md',
        'firmware/platform/README.md', 'communication/README.md', 'communication/host/README.md', 'communication/spec/README.md',
        'communication/spec/DEVICE_CONTRACT.md', 'communication/spec/VOFA_PROTOCOL.md',
        'firmware/targets/BLDC_G431/README.md',
        'firmware/targets/BLDC_G431/runtime/README.md',
        'firmware/targets/BLDC_G431/communication/README.md',
        'firmware/targets/ODRIVE_V36_405_M0/README.md',
        'firmware/targets/BLDC_G431/validation/README.md',
        'hardware/BLDC_G431/HARDWARE_BASELINE.md'
    ) | ForEach-Object { Write-Output (Join-Path $repositoryRoot $_) }
}
finally {
    if ($null -eq $previousLazyFetch) {
        Remove-Item Env:GIT_NO_LAZY_FETCH -ErrorAction SilentlyContinue
    } else {
        $env:GIT_NO_LAZY_FETCH = $previousLazyFetch
    }
}
