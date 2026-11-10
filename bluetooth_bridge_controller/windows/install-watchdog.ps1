[CmdletBinding()]
param([string]$MacAddress='100.64.0.10',[int]$MacPort=8767,[string]$EnrollmentToken='')
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
if(-not [Environment]::Is64BitProcess){
    & "$env:WINDIR\Sysnative\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -MacAddress $MacAddress -MacPort $MacPort -EnrollmentToken $EnrollmentToken
    if($LASTEXITCODE -ne 0){throw 'Native 64-bit installation failed'}
    exit 0
}
if(-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Administrator PowerShell is required'}
$octets=$MacAddress.Split('.')
if($octets.Count -ne 4 -or $octets[0] -ne '100' -or [int]$octets[1] -lt 64 -or [int]$octets[1] -gt 127){throw 'Use the Mac Tailscale IPv4 address'}
$healthUrl="http://${MacAddress}:$MacPort/agent/health"
try{$health=Invoke-RestMethod $healthUrl -TimeoutSec 5;if(!$health.ok -or $health.service -ne 'bluetooth-bridge'){throw 'not ready'}}catch{throw 'Mac health endpoint is not ready; upgrade and verify the Mac controller first'}
$dir=Join-Path $env:ProgramData 'BluetoothBridgeWatchdog'
New-Item -ItemType Directory $dir -Force | Out-Null
$acl=Get-Acl $dir
$acl.SetAccessRuleProtection($true,$false)
$acl.Access | ForEach-Object {$acl.RemoveAccessRule($_) | Out-Null}
foreach($sid in @('S-1-5-18','S-1-5-32-544')){
    $identity=New-Object Security.Principal.SecurityIdentifier($sid)
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow')))
}
Set-Acl $dir $acl
$tailscaleExe='C:\Program Files\Tailscale\tailscale.exe'
if(!(Test-Path $tailscaleExe)){throw 'Tailscale is not installed'}
$policy='HKLM:\SOFTWARE\Policies\Tailscale'
$previousMode=(Get-ItemProperty $policy -Name UnattendedMode -ErrorAction SilentlyContinue).UnattendedMode
if($previousMode -eq 'never'){throw 'Existing unattended policy forbids this operation'}
$policyBackup=Join-Path $dir 'tailscale-policy.previous.json'
if(!(Test-Path $policyBackup)){@{path=$policy;existed=($null -ne $previousMode);value=$previousMode}|ConvertTo-Json|Set-Content $policyBackup -Encoding UTF8}
New-Item $policy -Force|Out-Null
New-ItemProperty $policy -Name UnattendedMode -PropertyType String -Value always -Force|Out-Null
$null=& $tailscaleExe syspolicy reload 2>&1
if($LASTEXITCODE -ne 0){throw 'Tailscale policy reload failed'}
& $tailscaleExe set --unattended=true
if($LASTEXITCODE -ne 0){throw 'Tailscale unattended setup failed'}
$reportToken=''
if($EnrollmentToken){
    # WireGuard protects transport. A single-use grant is restricted to this peer.
    $enrolled=Invoke-RestMethod -Method Post -Uri "http://${MacAddress}:$MacPort/agent/watchdog/enroll" -Headers @{Authorization="Bearer $EnrollmentToken"} -ContentType 'application/json' -Body '{}' -TimeoutSec 20
    $chatId=[string]$enrolled.chat_id;$token=[string]$enrolled.token;$reportToken=[string]$enrolled.report_token
    $enrolled=$null;$EnrollmentToken=$null
}else{
$chatId=Read-Host 'Your private Telegram Chat ID'
$secureToken=Read-Host 'Telegram Bot Token (hidden)' -AsSecureString
$bstr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try{$token=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)}finally{[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)}
}
if($chatId -notmatch '^[1-9][0-9]*$'){throw 'Only a private Chat ID is allowed'}
try{
    $chat=Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$token/getChat" -ContentType 'application/json' -Body (@{chat_id=$chatId}|ConvertTo-Json -Compress) -TimeoutSec 15
    if(!$chat.ok -or $chat.result.type -ne 'private' -or [string]$chat.result.id -ne $chatId){throw 'not private'}
}catch{throw 'Telegram verification failed; check Token, private Chat ID and /start'}
Add-Type -AssemblyName System.Security
$plainBytes=[Text.Encoding]::UTF8.GetBytes($token)
try{$cipher=[Security.Cryptography.ProtectedData]::Protect($plainBytes,$null,[Security.Cryptography.DataProtectionScope]::LocalMachine)}finally{[Array]::Clear($plainBytes,0,$plainBytes.Length);$token=$null}
$config=@{health_url=$healthUrl;chat_id=$chatId;token_cipher=[Convert]::ToBase64String($cipher);report_token=$reportToken;report_url="http://${MacAddress}:$MacPort/agent/watchdog/report"}
$configPath=Join-Path $dir 'config.json'
$config|ConvertTo-Json|Set-Content $configPath -Encoding UTF8
$watchdog=@'
param([string]$ConfigPath)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Security
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
$cfg=Get-Content $ConfigPath -Raw|ConvertFrom-Json
$bytes=[Security.Cryptography.ProtectedData]::Unprotect([Convert]::FromBase64String($cfg.token_cipher),$null,[Security.Cryptography.DataProtectionScope]::LocalMachine)
try{$botToken=[Text.Encoding]::UTF8.GetString($bytes)}finally{[Array]::Clear($bytes,0,$bytes.Length)}
function Send-BridgeNotice([string]$Message){
    try{
        $result=Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$botToken/sendMessage" -ContentType 'application/json' -Body (@{chat_id=$cfg.chat_id;text=$Message}|ConvertTo-Json -Compress) -TimeoutSec 10
        return [bool]$result.ok
    }catch{return $false}
}
$failures=0;$alerted=$false;$nextAttempt=[DateTime]::MinValue;$lastState='starting';$recoverySent=$false;$startupSent=$false;$lastAlertAt='';$lastRecoveryAt=''
$statusPath=Join-Path (Split-Path $ConfigPath) 'status.json'
if(Test-Path $statusPath){
    try{$prior=Get-Content $statusPath -Raw|ConvertFrom-Json;$alerted=[bool]$prior.alert_sent;$startupSent=[bool]$prior.startup_sent;$recoverySent=[bool]$prior.recovery_sent;$lastAlertAt=[string]$prior.last_alert_at;$lastRecoveryAt=[string]$prior.last_recovery_at}catch{}
}
while($true){
    if(!$startupSent){$startupSent=Send-BridgeNotice 'Mac Bluetooth bridge: independent cloud watchdog started.'}
    $healthy=$false
    try{$r=Invoke-RestMethod $cfg.health_url -TimeoutSec 5;$healthy=$r.ok -and $r.service -eq 'bluetooth-bridge'}catch{}
    if($healthy){
        $failures=0
        if($alerted -and [DateTime]::UtcNow -ge $nextAttempt){
            if(Send-BridgeNotice 'Mac Bluetooth bridge recovered: the health endpoint is reachable again.'){$alerted=$false;$recoverySent=$true;$lastRecoveryAt=[DateTime]::UtcNow.ToString('o');$nextAttempt=[DateTime]::MinValue}else{$nextAttempt=[DateTime]::UtcNow.AddMinutes(1)}
        }
        $lastState='up'
    }else{
        $failures++
        $lastState='checking'
        if($failures -ge 3){
            $lastState='down'
            if(!$alerted -and [DateTime]::UtcNow -ge $nextAttempt){
                $alerted=Send-BridgeNotice 'Mac Bluetooth bridge unavailable: check Mac power, Tailscale, controller and the USB adapter.'
                $recoverySent=$false
                if($alerted){$lastAlertAt=[DateTime]::UtcNow.ToString('o');$nextAttempt=[DateTime]::MinValue}else{$nextAttempt=[DateTime]::UtcNow.AddMinutes(5)}
            }
        }
    }
    $report=@{state=$lastState;consecutive_failures=$failures;alert_sent=$alerted;recovery_sent=$recoverySent;startup_sent=$startupSent;last_alert_at=$lastAlertAt;last_recovery_at=$lastRecoveryAt;checked_at=[DateTime]::UtcNow.ToString('o')}|ConvertTo-Json -Compress
    $temporary=$statusPath+'.tmp';$report|Set-Content $temporary -Encoding UTF8;Move-Item $temporary $statusPath -Force
    if($cfg.report_token){try{$null=Invoke-RestMethod -Method Post -Uri $cfg.report_url -Headers @{Authorization="Bearer $($cfg.report_token)"} -ContentType 'application/json' -Body $report -TimeoutSec 5}catch{}}
    Start-Sleep -Seconds 15
}
'@
$scriptPath=Join-Path $dir 'watchdog.ps1'
$watchdog|Set-Content $scriptPath -Encoding UTF8
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`" -ConfigPath `"$configPath`""
$triggers=@((New-ScheduledTaskTrigger -AtStartup),(New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)))
$principal=New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
Stop-ScheduledTask -TaskName 'Mac Bluetooth Bridge Watchdog' -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName 'Mac Bluetooth Bridge Watchdog' -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force|Out-Null
Start-ScheduledTask -TaskName 'Mac Bluetooth Bridge Watchdog'
Write-Output 'WATCHDOG-INSTALLED: check Telegram for the startup test notice.'
