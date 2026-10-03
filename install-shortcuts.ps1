$ErrorActionPreference='Stop'
$appRoot=$PSScriptRoot
if (-not (Test-Path -LiteralPath (Join-Path $appRoot 'settings.json'))) {
    throw '请先复制 settings.example.json 为 settings.json，并填写自己的学习资料目录。'
}
$shortcutShell=New-Object -ComObject WScript.Shell
$psExe=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
foreach ($entry in @(@{folder='Desktop';startup=$false},@{folder='Startup';startup=$true})) {
    $shortcutPath=Join-Path ([Environment]::GetFolderPath($entry.folder)) '孙权.lnk'
    $shortcut=$shortcutShell.CreateShortcut($shortcutPath)
    if ((Test-Path -LiteralPath $shortcutPath) -and $shortcut.Arguments -notlike ('*'+$appRoot+'\launch.ps1*')) {
        throw ('已有其他程序的同名入口，未覆盖：'+$shortcutPath)
    }
    $shortcut.TargetPath=$psExe
    $shortcut.Arguments='-NoLogo -NoProfile -WindowStyle Hidden -File "'+(Join-Path $appRoot 'launch.ps1')+'"'+$(if ($entry.startup) {' -Startup'} else {''})
    $shortcut.WorkingDirectory=$appRoot
    $shortcut.WindowStyle=7
    $shortcut.IconLocation=(Join-Path $appRoot 'web\sunquan.ico')+',0'
    $shortcut.Description='孙权 · 本地免费学习搭子'
    $shortcut.Save()
    Write-Output $shortcutPath
}
Write-Output '已创建桌面与当前用户登录启动入口。关闭开机提醒可运行 disable-startup.ps1。'
