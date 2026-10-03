$ErrorActionPreference='Stop'
$path=Join-Path ([Environment]::GetFolderPath('Startup')) '孙权.lnk'
if (Test-Path -LiteralPath $path) {
    $shell=New-Object -ComObject WScript.Shell
    $shortcut=$shell.CreateShortcut($path)
    if ($shortcut.Arguments -like ('*'+$PSScriptRoot+'*')) {
        Remove-Item -LiteralPath $path
        Write-Host '已关闭下次登录时的学习提醒。记忆、资料与模型仍然保留。'
    } else { throw '这个同名快捷方式不属于当前学习搭子，未改动。' }
} else { Write-Host '开机提醒已关闭。' }
