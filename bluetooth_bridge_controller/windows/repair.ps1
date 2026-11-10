$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$dir=Join-Path $env:ProgramData 'BluetoothBridge'
$cfgPath=Join-Path $dir 'agent.json'
if(!(Test-Path $cfgPath)){throw 'AgentConfigMissing'}
$cfg=Get-Content $cfgPath -Raw | ConvertFrom-Json
if($cfg.poll_url -ne '{{CONTROLLER_BASE}}/agent/poll'){throw 'ControllerIdentityMismatch'}
$tailscaleExe='C:\Program Files\Tailscale\tailscale.exe'
if(!(Test-Path $tailscaleExe)){throw 'TailscaleMissing'}
$ts=(& $tailscaleExe status --json 2>$null | Out-String) | ConvertFrom-Json
if($ts.BackendState -ne 'Running'){throw 'TailscaleNotReady'}
$null=& $tailscaleExe set --unattended=true 2>&1
if($LASTEXITCODE -ne 0){throw 'TailscaleUnattendedSettingFailed'}
$pattern='VID_'+$cfg.usb_vid+'&PID_'+$cfg.usb_pid
if(Get-PnpDevice -PresentOnly -ErrorAction Stop | Where-Object {$_.InstanceId -like "*$pattern*"}){throw 'ReleaseUsbBeforeRepair'}
$agent=Join-Path $dir 'agent.ps1'
$tmp=Join-Path $dir 'agent.fixed.ps1'
Invoke-WebRequest -UseBasicParsing '{{CONTROLLER_BASE}}/agent/releases/{{AGENT_SHA256}}/agent.ps1' -OutFile $tmp -TimeoutSec 20
if((Get-FileHash $tmp -Algorithm SHA256).Hash.ToLowerInvariant() -ne '{{AGENT_SHA256}}'){throw 'AgentIntegrityFailure'}
$parseTokens=$null;$parseErrors=$null
$null=[Management.Automation.Language.Parser]::ParseFile($tmp,[ref]$parseTokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'AgentParseFailure'}
Stop-ScheduledTask -TaskName 'Mac Bluetooth Bridge Agent' -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Copy-Item $agent (Join-Path $dir 'agent.previous.ps1') -Force
Move-Item $tmp $agent -Force
$escaped=[regex]::Escape([string]$cfg.virtualhere_exe)
Get-CimInstance Win32_Service | Where-Object {$_.PathName -match ('(?i)^"?'+$escaped+'(?:"|\s|$)')} | ForEach-Object {Start-Service -Name $_.Name}
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$agent`" -ConfigPath `"$cfgPath`""
$bootTrigger=New-ScheduledTaskTrigger -AtStartup
$repeatTrigger=New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
$trigger=@($bootTrigger,$repeatTrigger)
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'Mac Bluetooth Bridge Agent' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'Mac Bluetooth Bridge Agent'
Write-Output 'BRIDGE-REPAIR-COMMAND-FINISHED'
