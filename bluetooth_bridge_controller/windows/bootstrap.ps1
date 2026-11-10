[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ControllerBase,
    [Parameter(Mandatory=$true)][string]$TailscaleAuthKey,
    [Parameter(Mandatory=$true)][string]$EnrollmentToken,
    [Parameter(Mandatory=$true)][string]$ComputerName,
    [Parameter(Mandatory=$true)][string]$AgentSha256,
    [string]$TailscaleMsiUrl = 'https://pkgs.tailscale.com/stable/tailscale-setup-1.104.5-amd64.msi',
    [string]$VirtualHereUrl = 'https://www.virtualhere.com/sites/default/files/usbclient/vhui64.exe'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$baseDir = Join-Path $env:ProgramData 'BluetoothBridge'
$tailscaleMsi = Join-Path $env:TEMP 'tailscale-setup-amd64.msi'
$vhExe = Join-Path $baseDir 'vhui64.exe'
$agentPath = Join-Path $baseDir 'agent.ps1'
$configPath = Join-Path $baseDir 'agent.json'
New-Item -ItemType Directory -Path $baseDir -Force | Out-Null
if (Test-Path -LiteralPath $configPath) { throw 'Bridge agent is already configured; use its existing registration or an explicit repair procedure.' }

$acl = Get-Acl -LiteralPath $baseDir
$acl.SetAccessRuleProtection($true, $false)
$acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
foreach ($sid in @('S-1-5-18','S-1-5-32-544')) {
    $identity = New-Object Security.Principal.SecurityIdentifier($sid)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $baseDir -AclObject $acl

function Assert-Signature {
    param([string]$Path, [string]$PublisherPattern)
    $sig = Get-AuthenticodeSignature -LiteralPath $Path
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch $PublisherPattern) {
        throw "Signature verification failed for $Path ($($sig.Status), $($sig.SignerCertificate.Subject))"
    }
}

Set-Service -Name 'iphlpsvc' -StartupType Automatic
Start-Service -Name 'iphlpsvc'

$tailscaleExe = 'C:\Program Files\Tailscale\tailscale.exe'
if (-not (Test-Path -LiteralPath $tailscaleExe)) {
    # Some Wuying images disable dependencies checked by the Tailscale MSI.
    # Only re-enable disabled dependencies; preserve existing enabled settings.
    foreach ($serviceName in @('Dnscache','iphlpsvc','netprofm','WinHttpAutoProxySvc')) {
        $dependency=Get-Service -Name $serviceName -ErrorAction Stop
        if ($dependency.StartType -eq 'Disabled') {
            Set-Service -Name $serviceName -StartupType Manual -ErrorAction Stop
        }
    }
    Invoke-WebRequest -UseBasicParsing -Uri $TailscaleMsiUrl -OutFile $tailscaleMsi
    Assert-Signature -Path $tailscaleMsi -PublisherPattern 'Tailscale Inc\.'
    $install = Start-Process msiexec.exe -ArgumentList @('/i',('"'+$tailscaleMsi+'"'),'/qn','/norestart','TS_NOLAUNCH=1') -Wait -PassThru
    if ($install.ExitCode -notin 0,3010) { throw "Tailscale installation failed: $($install.ExitCode)" }
}

$machinePolicy='HKLM:\SOFTWARE\Policies\Tailscale'
$previousMode=(Get-ItemProperty $machinePolicy -Name UnattendedMode -ErrorAction SilentlyContinue).UnattendedMode
if($previousMode -eq 'never'){throw 'ExistingUnattendedPolicyConflict'}
$policyBackup=Join-Path $baseDir 'tailscale-policy.previous.json'
if(!(Test-Path $policyBackup)){@{path=$machinePolicy;existed=($null -ne $previousMode);value=$previousMode} | ConvertTo-Json | Set-Content $policyBackup -Encoding UTF8}
New-Item $machinePolicy -Force | Out-Null
New-ItemProperty $machinePolicy -Name UnattendedMode -PropertyType String -Value always -Force | Out-Null
$null=& $tailscaleExe syspolicy reload 2>&1
if($LASTEXITCODE -ne 0){throw 'TailscalePolicyReloadFailed'}
& $tailscaleExe up --auth-key=$TailscaleAuthKey --hostname=$ComputerName --advertise-tags=tag:wuying --accept-dns=false --unattended=true --timeout=60s
if ($LASTEXITCODE -ne 0) { throw "Tailscale enrollment failed: $LASTEXITCODE" }

$tailscaleIp = ''
for ($i=0; $i -lt 30 -and -not $tailscaleIp; $i++) {
    $tailscaleIp = ([string](& $tailscaleExe ip -4 2>$null | Select-Object -First 1)).Trim()
    if (-not $tailscaleIp) { Start-Sleep -Seconds 2 }
}
if (-not $tailscaleIp) { throw 'Tailscale IPv4 address was not assigned' }

# Verify controller reachability and agent integrity before installing a USB
# service or consuming the single-use controller enrollment token.
Invoke-WebRequest -UseBasicParsing -Uri "$ControllerBase/agent/releases/$AgentSha256/agent.ps1" -OutFile $agentPath -TimeoutSec 20
$actualAgentHash = (Get-FileHash -LiteralPath $agentPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualAgentHash -ne $AgentSha256.ToLowerInvariant()) { throw "Agent hash mismatch: $actualAgentHash" }

$existingService = Get-CimInstance Win32_Service | Where-Object { $_.PathName -match 'vhui(32|64)\.exe' } | Select-Object -First 1
if ($existingService) {
    $servicePath = [regex]::Match($existingService.PathName, '(?i)^"?(.+?vhui(?:32|64)\.exe)').Groups[1].Value
    Assert-Signature -Path $servicePath -PublisherPattern 'VirtualHere'
    $vhExe = $servicePath
    Start-Service -Name $existingService.Name
} else {
    if (Get-Process -Name vhui64,vhui32 -ErrorAction SilentlyContinue) {
        throw 'VirtualHere GUI is running. Close it before installing the background service.'
    }
    Invoke-WebRequest -UseBasicParsing -Uri $VirtualHereUrl -OutFile $vhExe
    Assert-Signature -Path $vhExe -PublisherPattern 'VirtualHere'
    $install = Start-Process -FilePath $vhExe -ArgumentList '-b' -PassThru
    if (-not $install.WaitForExit(30000)) { throw 'VirtualHere service install timed out; inspect before retrying.' }
    if ($install.ExitCode -ne 0) { throw "VirtualHere service install failed: $($install.ExitCode)" }
    Start-Sleep -Seconds 5
}

# The dedicated bridge client must never reconnect to USB without a live lease.
$vhResult = Join-Path $baseDir 'clear-auto-use.txt'
Remove-Item -LiteralPath $vhResult -Force -ErrorAction SilentlyContinue
$clearAuto = Start-Process -FilePath $vhExe -ArgumentList @('-t','"AUTO USE CLEAR ALL"','-r',('"'+$vhResult+'"')) -PassThru
if (-not $clearAuto.WaitForExit(10000)) { throw 'VirtualHere auto-use reset timed out' }
if (-not (Test-Path $vhResult) -or (Get-Content $vhResult -Raw).Trim() -ne 'OK') { throw 'Could not confirm VirtualHere auto-use is disabled' }

$machineGuid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
$biosUuid = (Get-CimInstance Win32_ComputerSystemProduct).UUID
$identityBytes = [Text.Encoding]::UTF8.GetBytes("$machineGuid|$biosUuid")
$sha256 = [Security.Cryptography.SHA256]::Create()
try { $identityHash = -join ($sha256.ComputeHash($identityBytes) | ForEach-Object { $_.ToString('x2') }) }
finally { $sha256.Dispose() }

$enrollBody = @{
    enrollment_token = $EnrollmentToken
    machine_id = $identityHash
    hostname = $env:COMPUTERNAME
    tailscale_ip = $tailscaleIp
} | ConvertTo-Json -Compress
$enrollment = Invoke-RestMethod -Method Post -Uri "$ControllerBase/agent/enroll" -ContentType 'application/json' -Body $enrollBody -TimeoutSec 20

$agentConfig = @{
    agent_id = $enrollment.agent_id
    agent_token = $enrollment.agent_token
    poll_url = $enrollment.poll_url
    hub = $enrollment.hub
    usb_name = $enrollment.usb_name
    usb_vid = $enrollment.usb_vid
    usb_pid = $enrollment.usb_pid
    poll_seconds = $enrollment.poll_seconds
    virtualhere_exe = $vhExe
    applied_generation = -1
    applied_action = 'unknown'
    vh_status = 'unknown'
    bluetooth_status = 'absent'
    last_device_address = ''
    error = ''
}
$agentConfig | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $configPath -Encoding UTF8

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$agentPath`" -ConfigPath `"$configPath`""
$bootTrigger = New-ScheduledTaskTrigger -AtStartup
$repeatTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
$trigger = @($bootTrigger, $repeatTrigger)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -RestartCount 20 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'Mac Bluetooth Bridge Agent' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName 'Mac Bluetooth Bridge Agent'

$TailscaleAuthKey = $null
$EnrollmentToken = $null
Remove-Item -LiteralPath $tailscaleMsi -Force -ErrorAction SilentlyContinue
Write-Output "BLUETOOTH-BRIDGE-ENROLLED agent=$($enrollment.agent_id) tailscale=$tailscaleIp"
