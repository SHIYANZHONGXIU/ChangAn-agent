$ErrorActionPreference = 'Stop'

$userRoot = $env:LOCALAPPDATA
if (-not $userRoot) { throw 'Windows 用户数据目录不可用。' }
$programRoot = Join-Path $userRoot 'Programs'
$installRoot = Join-Path $programRoot 'ChangAn'
$dataHome = Join-Path $userRoot 'ChangAn'
$dataTarget = Join-Path $dataHome 'data'
$sourceApp = $PSScriptRoot
$sourceExe = Join-Path $sourceApp 'ChangAn.exe'

if (-not (Test-Path -LiteralPath $sourceExe -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $sourceApp '_internal') -PathType Container)) {
    throw '没有找到完整的长安桌面版程序。请先完整解压 ZIP，再运行本脚本。'
}
if (Test-Path -LiteralPath $installRoot) {
    throw "安装目录已存在，未覆盖任何文件：$installRoot`n如需升级，请先备份数据，退出长安并删除旧程序文件夹。"
}

$programStage = Join-Path $programRoot ('.ChangAn-install-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $programRoot -Force | Out-Null
New-Item -ItemType Directory -Path $programStage | Out-Null
$importStage = $null
$importLock = $null
$installed = $false

function Get-VerifiedHash([string]$path) {
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($path)
    try { ([System.BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '') }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

function Copy-VerifiedTree([string]$source, [string]$target) {
    $sourceRoot = [System.IO.Path]::GetFullPath($source)
    New-Item -ItemType Directory -Path $target -Force | Out-Null
    $items = New-Object System.Collections.ArrayList
    $pending = New-Object System.Collections.Stack
    $pending.Push($sourceRoot)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        foreach ($child in (Get-ChildItem -LiteralPath $current -Force)) {
            if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "为防止目录链接带入未选定位置，安装已停止：$($child.FullName)"
            }
            [void]$items.Add($child)
            if ($child.PSIsContainer) { $pending.Push($child.FullName) }
        }
    }
    foreach ($item in $items) {
        $relative = $item.FullName.Substring($sourceRoot.Length).TrimStart([char[]]@('\', '/'))
        $destination = Join-Path $target $relative
        if ($item.PSIsContainer) {
            New-Item -ItemType Directory -Path $destination -Force | Out-Null
        } else {
            New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $destination
            $sourceHash = Get-VerifiedHash $item.FullName
            $copiedHash = Get-VerifiedHash $destination
            if ($sourceHash -ne $copiedHash) { throw "文件复制校验失败：$relative" }
        }
    }
}

try {
    Write-Host '正在复制长安程序并校验文件…' -ForegroundColor Cyan
    Copy-VerifiedTree $sourceApp $programStage

    $existingData = Test-Path -LiteralPath $dataTarget
    $existingDataItems = if ($existingData) { @(Get-ChildItem -LiteralPath $dataTarget -Force) } else { @() }
    if ($existingDataItems.Count -gt 0) {
        Write-Host "已保留现有数据目录，不会合并或覆盖：$dataTarget" -ForegroundColor Yellow
    } else {
        $defaultLegacy = Join-Path $env:USERPROFILE 'Desktop\Agent\data'
        $response = Read-Host '是否要导入旧长安的聊天、密钥设置和记忆？请先关闭旧程序后再选择“是”。输入 Y 导入；直接回车使用空白数据目录'
        if ($response -match '^(?i:y|yes|是)$') {
            Add-Type -AssemblyName System.Windows.Forms
            $picker = New-Object System.Windows.Forms.FolderBrowserDialog
            $picker.Description = '选择旧长安的 data 数据文件夹。不会删除或修改原目录。'
            $picker.ShowNewFolderButton = $false
            if (Test-Path -LiteralPath $defaultLegacy -PathType Container) {
                $picker.SelectedPath = $defaultLegacy
            }
            if ($picker.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
                throw '已取消数据导入；没有复制或删除用户数据。'
            }
            $importSource = [System.IO.Path]::GetFullPath($picker.SelectedPath)
            if ($importSource -eq [System.IO.Path]::GetFullPath($dataTarget)) {
                throw '选定的文件夹已是新程序数据目录，无需导入。'
            }
            if (-not (Test-Path -LiteralPath (Join-Path $importSource 'workspace.json') -PathType Leaf)) {
                throw '选择的文件夹缺少 workspace.json，不像是长安的完整数据目录；原目录未修改。'
            }
            $lockPath = Join-Path $importSource '.changan.lock'
            if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
                throw '没有找到数据目录的运行锁，无法确认旧程序是否已退出；为保护聊天和记忆，已取消导入。'
            }
            $importLock = [System.IO.File]::Open($lockPath, [System.IO.FileMode]::Open,
                [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::ReadWrite)
            try { $importLock.Lock(0, 1) }
            catch {
                $importLock.Dispose()
                $importLock = $null
                throw '检测到旧长安仍在使用此数据目录；请退出旧程序后重试。'
            }
            $importStage = Join-Path $dataHome ('.data-import-' + [guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Path $dataHome -Force | Out-Null
            Copy-VerifiedTree $importSource $importStage
        }
    }

    if (Test-Path -LiteralPath $dataTarget) {
        if (@(Get-ChildItem -LiteralPath $dataTarget -Force).Count -gt 0 -and $importStage) {
            throw "安装期间发现已有用户数据；为保护数据，未合并本次导入。保留原数据和复制副本：$dataTarget"
        }
    }

    if ($importStage) {
        if (Test-Path -LiteralPath $dataTarget) { Remove-Item -LiteralPath $dataTarget }
        Move-Item -LiteralPath $importStage -Destination $dataTarget
        $importStage = $null
    } elseif (-not $existingDataItems.Count -or -not (Test-Path -LiteralPath $dataTarget)) {
        New-Item -ItemType Directory -Path $dataTarget -Force | Out-Null
    }

    Move-Item -LiteralPath $programStage -Destination $installRoot
    $installed = $true
    $exe = Join-Path $installRoot 'ChangAn.exe'
    $shell = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath('DesktopDirectory')
    $startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) '长安'
    New-Item -ItemType Directory -Path $startMenu -Force | Out-Null
    foreach ($shortcutPath in @((Join-Path $desktop '长安.lnk'), (Join-Path $startMenu '长安.lnk'))) {
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $exe
        $shortcut.WorkingDirectory = $installRoot
        $shortcut.IconLocation = "$exe,0"
        $shortcut.Description = '长安本地多模型 Agent'
        $shortcut.Save()
    }
    $uninstaller = $shell.CreateShortcut((Join-Path $startMenu '卸载长安.lnk'))
    $uninstaller.TargetPath = Join-Path $PSHOME 'powershell.exe'
    $uninstaller.Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $installRoot 'Uninstall-ChangAn.ps1')`""
    $uninstaller.WorkingDirectory = $env:TEMP
    $uninstaller.Description = '卸载长安（保留聊天和设置）'
    $uninstaller.Save()
    Write-Host "长安已安装，桌面和开始菜单已创建快捷方式。`n程序：$installRoot`n数据：$dataTarget`n原始数据仍保留在原位置。" -ForegroundColor Green
    Read-Host '按回车关闭安装窗口'
} catch {
    if ($installed) {
        Write-Warning "程序文件可能已安装，但后续设置未完成。错误：$($_.Exception.Message)`n程序目录：$installRoot`n用户数据不会自动删除。"
    } else {
        Write-Warning "安装未完成，错误：$($_.Exception.Message)"
    }
    Read-Host '按回车关闭安装窗口'
    exit 1
} finally {
    if ($importLock) {
        try { $importLock.Unlock(0, 1) } catch { }
        $importLock.Dispose()
    }
    if (Test-Path -LiteralPath $programStage) { Remove-Item -LiteralPath $programStage -Recurse -Force }
    if ($importStage -and (Test-Path -LiteralPath $importStage)) {
        Write-Warning "保留数据导入副本供人工检查：$importStage"
    }
}
