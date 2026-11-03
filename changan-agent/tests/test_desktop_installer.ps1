$ErrorActionPreference = 'Stop'
$installPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\install_desktop.ps1'
$tokens = $null
$parseErrors = $null
$scriptAst = [System.Management.Automation.Language.Parser]::ParseFile($installPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw ($parseErrors | Out-String) }
$hashFunction = $scriptAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-VerifiedHash' }, $true)
$copyFunction = $scriptAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Copy-VerifiedTree' }, $true)
if (-not $hashFunction -or -not $copyFunction) { throw '没有找到安装程序的数据校验函数。' }
Invoke-Expression $hashFunction.Extent.Text
Invoke-Expression $copyFunction.Extent.Text

$root = Join-Path ([System.IO.Path]::GetTempPath()) ('ChangAn-installer-test-' + [guid]::NewGuid().ToString('N'))
try {
    $source = Join-Path $root 'source'
    $target = Join-Path $root 'target'
    $nested = Join-Path $source 'projects\example'
    New-Item -ItemType Directory -Path $nested -Force | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $source 'workspace.json'), '{"projects":[]}')
    [System.IO.File]::WriteAllText((Join-Path $nested 'chat.json'), '{"messages":["fake"]}')
    Copy-VerifiedTree $source $target
    foreach ($relative in @('workspace.json', 'projects\example\chat.json')) {
        $original = Get-VerifiedHash (Join-Path $source $relative)
        $copied = Get-VerifiedHash (Join-Path $target $relative)
        if ($original -ne $copied) { throw "临时目录复制摘要不一致：$relative" }
    }
    $lockPath = Join-Path $root '.changan.lock'
    [System.IO.File]::WriteAllBytes($lockPath, [byte[]]@(48))
    $owner = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
    $contender = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
    try {
        $owner.Lock(0, 1)
        $blocked = $false
        try { $contender.Lock(0, 1) } catch { $blocked = $true }
        if (-not $blocked) { throw '安装器的运行锁未能阻止并发写入。' }
        $owner.Unlock(0, 1)
        $contender.Lock(0, 1)
        $contender.Unlock(0, 1)
    } finally {
        $owner.Dispose()
        $contender.Dispose()
    }
    Write-Host '安装器复制函数、嵌套目录与文件校验通过；仅使用临时虚构文件。'
} finally {
    if (Test-Path -LiteralPath $root -PathType Container) { Remove-Item -LiteralPath $root -Recurse -Force }
}
