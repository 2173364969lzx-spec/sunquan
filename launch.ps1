param([switch]$Startup,[switch]$NoTray)
$ErrorActionPreference='Stop'
$appRoot=$PSScriptRoot
$configuration=Get-Content -LiteralPath (Join-Path $appRoot 'settings.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$base='http://127.0.0.1:'+$configuration.port
$python=Join-Path (Split-Path ((Get-Command python).Source) -Parent) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $python)) { $python=(Get-Command python).Source }
try { $null=Invoke-RestMethod -Uri ($base+'/health') -TimeoutSec 2 }
catch {
    Start-Process -FilePath $python -ArgumentList @('-X','utf8',('"'+(Join-Path $appRoot 'app.py')+'"')) -WorkingDirectory $appRoot -WindowStyle Hidden
    $ready=$false
    for ($attempt=0;$attempt -lt 30;$attempt++) {
        Start-Sleep -Milliseconds 300
        try { $null=Invoke-RestMethod -Uri ($base+'/health') -TimeoutSec 2; $ready=$true;break } catch {}
    }
    if (-not $ready) { throw '学习搭子未能启动。请运行 app.py 检查错误。' }
}
if (-not $Startup) { Start-Process $base }
if ($NoTray) { exit }
$created=$false
$mutex=[System.Threading.Mutex]::new($true,'Local\JiezhexueStudyCompanion',[ref]$created)
if (-not $created) { $mutex.Dispose();exit }
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$auth=@{Authorization=('Bearer '+(Get-Content -LiteralPath (Join-Path $appRoot 'data/local-token.txt') -Raw).Trim())}
function Send-Action([string]$action) {
    Invoke-RestMethod -Uri ($base+'/api/reminder') -Method Post -Headers $auth -ContentType 'application/json' -Body (@{action=$action}|ConvertTo-Json -Compress) -TimeoutSec 4 | Out-Null
}
function Open-Study([string]$query='') { Start-Process ($base+$query) }
$icon=[System.Windows.Forms.NotifyIcon]::new()
$appIconPath=Join-Path $appRoot 'web/sunquan.ico'
$appIcon=[System.Drawing.Icon]::new($appIconPath)
$icon.Icon=$appIcon
$icon.Text='孙权 · 本地学习搭子'
$menu=[System.Windows.Forms.ContextMenuStrip]::new()
$open=$menu.Items.Add('打开孙权');$open.Add_Click({ Open-Study })
$later=$menu.Items.Add('15 分钟后再提醒');$later.Add_Click({ Send-Action 'snooze';$timer.Interval=60000 })
$rest=$menu.Items.Add('今天休息');$rest.Add_Click({ Send-Action 'skip' })
$quit=$menu.Items.Add('退出')
$quit.Add_Click({
    try { Invoke-RestMethod -Uri ($base+'/api/shutdown') -Method Post -Headers $auth -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null } catch {}
    $timer.Stop();$icon.Visible=$false;[System.Windows.Forms.Application]::Exit()
})
$icon.ContextMenuStrip=$menu
$icon.Add_DoubleClick({ Open-Study })
$icon.Visible=$true
$trayStatus=Join-Path $appRoot 'data/tray-status.json'
@{pid=$PID;started_at=[DateTimeOffset]::Now.ToString('o');startup=[bool]$Startup;state='running';app='孙权';icon=$appIconPath} | ConvertTo-Json -Compress | Set-Content -LiteralPath $trayStatus -Encoding UTF8
$script:reminderForm=$null
function Show-StudyReminder([string]$body) {
    if ($script:reminderForm -and -not $script:reminderForm.IsDisposed) { return }
    $form=[System.Windows.Forms.Form]::new();$script:reminderForm=$form
    $form.Text='孙权';$form.Icon=$appIcon;$form.Size=[System.Drawing.Size]::new(510,305)
    $form.StartPosition='CenterScreen';$form.FormBorderStyle='FixedDialog';$form.MaximizeBox=$false;$form.MinimizeBox=$false
    $form.BackColor=[System.Drawing.ColorTranslator]::FromHtml('#F4F1E9')
    $form.Font=[System.Drawing.Font]::new('Microsoft YaHei UI',10)
    $heading=[System.Windows.Forms.Label]::new();$heading.Text='先开始 5 分钟就好。';$heading.Location=[System.Drawing.Point]::new(22,22);$heading.Size=[System.Drawing.Size]::new(430,32);$heading.Font=[System.Drawing.Font]::new('Microsoft YaHei UI',15,[System.Drawing.FontStyle]::Bold)
    $label=[System.Windows.Forms.Label]::new();$label.Text=if ($body.Length -gt 130) {$body.Substring(0,130)+'…'} else {$body};$label.Location=[System.Drawing.Point]::new(24,70);$label.Size=[System.Drawing.Size]::new(448,115)
    $go=[System.Windows.Forms.Button]::new();$go.Text='开始 5 分钟';$go.Location=[System.Drawing.Point]::new(24,205);$go.Size=[System.Drawing.Size]::new(104,36)
    $longer=[System.Windows.Forms.Button]::new();$longer.Text='学半小时';$longer.Location=[System.Drawing.Point]::new(138,205);$longer.Size=[System.Drawing.Size]::new(104,36)
    $easy=[System.Windows.Forms.Button]::new();$easy.Text='换个轻松的';$easy.Location=[System.Drawing.Point]::new(252,205);$easy.Size=[System.Drawing.Size]::new(104,36)
    $skip=[System.Windows.Forms.Button]::new();$skip.Text='今天休息';$skip.Location=[System.Drawing.Point]::new(366,205);$skip.Size=[System.Drawing.Size]::new(104,36)
    $go.Add_Click({ Open-Study '?study=5';$script:reminderForm.Close() })
    $longer.Add_Click({ Open-Study '?study=30';$script:reminderForm.Close() })
    $easy.Add_Click({ Open-Study '?study=5&easy=1';$script:reminderForm.Close() })
    $skip.Add_Click({ Send-Action 'skip';$script:reminderForm.Close() })
    $form.Controls.AddRange(@($heading,$label,$go,$longer,$easy,$skip));$form.Show()
    @{pid=$PID;shown_at=[DateTimeOffset]::Now.ToString('o');state='reminder-shown';app='孙权';icon=$appIconPath} | ConvertTo-Json -Compress | Set-Content -LiteralPath $trayStatus -Encoding UTF8
}
$timer=[System.Windows.Forms.Timer]::new();$timer.Interval=45000
$timer.Add_Tick({
    try {
        $r=Invoke-RestMethod -Uri ($base+'/api/reminder') -Headers $auth -TimeoutSec 3
        if ($script:reminderForm -and -not $script:reminderForm.IsDisposed -and $r.status -in @('start','snooze','skip')) { $script:reminderForm.Close() }
        if ($r.should_show) { Send-Action 'shown';Show-StudyReminder $r.body }
        $timer.Interval=if ($script:reminderForm -and -not $script:reminderForm.IsDisposed) {5000} elseif ($r.status -eq 'snooze') {60000} else {600000}
    } catch { $timer.Interval=60000 }
})
$timer.Start()
try { [System.Windows.Forms.Application]::Run() }
finally { $timer.Dispose();$icon.Dispose();$appIcon.Dispose();$mutex.ReleaseMutex();$mutex.Dispose();Remove-Item -LiteralPath $trayStatus -ErrorAction SilentlyContinue }
