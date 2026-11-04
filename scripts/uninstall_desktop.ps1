$ErrorActionPreference = 'Stop'
$userRoot = [System.IO.Path]::GetFullPath($env:LOCALAPPDATA)
$programRoot = [System.IO.Path]::GetFullPath((Join-Path $userRoot 'Programs'))
$installRoot = [System.IO.Path]::GetFullPath((Join-Path $programRoot 'ChangAn'))
if (-not $installRoot.StartsWith($programRoot + [System.IO.Path]::DirectorySeparatorChar,
        [System.StringComparison]::OrdinalIgnoreCase)) {
    throw '程序目录校验失败，未删除任何文件。'
}
if (-not (Test-Path -LiteralPath $installRoot -PathType Container)) {
    Write-Host "没有找到长安程序目录：$installRoot"
    exit 0
}
$running = Get-CimInstance Win32_Process -Filter "Name = 'ChangAn.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($installRoot + '\', [System.StringComparison]::OrdinalIgnoreCase) }
if ($running) { throw '请先关闭长安，再运行卸载。' }
$confirmation = Read-Host '这只会删除程序文件和长安快捷方式，不会删除聊天、记忆或设置。输入 DELETE 确认'
if ($confirmation -cne 'DELETE') {
    Write-Host '已取消，没有删除任何文件。'
    exit 0
}
$expectedExe = Join-Path $installRoot 'ChangAn.exe'
foreach ($shortcutPath in @(
        (Join-Path ([Environment]::GetFolderPath('DesktopDirectory')) '长安.lnk'),
        (Join-Path ([Environment]::GetFolderPath('Programs')) '长安\长安.lnk'))) {
    if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
        $target = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath).TargetPath
        if ([System.IO.Path]::GetFullPath($target) -ieq $expectedExe) {
            Remove-Item -LiteralPath $shortcutPath
        }
    }
}
$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) '长安'
if ((Test-Path -LiteralPath $startMenu -PathType Container) -and
    (@(Get-ChildItem -LiteralPath $startMenu -Force).Count -eq 0)) {
    Remove-Item -LiteralPath $startMenu
}
Remove-Item -LiteralPath $installRoot -Recurse -Force
Write-Host "已移除程序；用户数据仍保留在 $(Join-Path $userRoot 'ChangAn\data')。"
