[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ExpectedMachineHash,
    [Parameter(Mandatory=$true)][int]$ExpectedAgentId
)
$ErrorActionPreference='Stop'
if([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne 'S-1-5-18'){throw 'SystemContextRequired'}
$dir=Join-Path $env:ProgramData 'BluetoothBridge'
$cfg=Get-Content (Join-Path $dir 'agent.json') -Raw | ConvertFrom-Json
$guid=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
$uuid=(Get-CimInstance Win32_ComputerSystemProduct).UUID
$sha=[Security.Cryptography.SHA256]::Create()
try{$machineHash=-join($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes("$guid|$uuid")) | ForEach-Object {$_.ToString('x2')})}finally{$sha.Dispose()}
if($machineHash -ne $ExpectedMachineHash -or [int]$cfg.agent_id -ne $ExpectedAgentId){throw 'NetworkRecoveryIdentityMismatch'}
$policy='HKLM:\SOFTWARE\Policies\Tailscale'
$old=(Get-ItemProperty $policy -Name UnattendedMode -ErrorAction SilentlyContinue).UnattendedMode
if($old -eq 'never'){throw 'ExistingUnattendedPolicyConflict'}
$backup=Join-Path $dir 'tailscale-policy.previous.json'
if(!(Test-Path $backup)){@{path=$policy;existed=($null -ne $old);value=$old} | ConvertTo-Json | Set-Content $backup -Encoding UTF8}
New-Item $policy -Force | Out-Null
New-ItemProperty $policy -Name UnattendedMode -PropertyType String -Value always -Force | Out-Null
$exe='C:\Program Files\Tailscale\tailscale.exe'
$ui='C:\Program Files\Tailscale\tailscale-ipn.exe'
Get-Process tailscale-ipn -ErrorAction SilentlyContinue | Where-Object {$_.Path -eq $ui} | Stop-Process -Force
$before=(& $exe status --json 2>$null | Out-String) | ConvertFrom-Json
if($old -ne 'always' -or $before.BackendState -ne 'Running'){Restart-Service Tailscale -ErrorAction Stop;Start-Sleep -Seconds 3}
$state=(& $exe status --json 2>$null | Out-String) | ConvertFrom-Json
if($state.BackendState -ne 'Running'){
    $list=(& $exe switch --list 2>$null | Out-String)
    $profiles=@($list -split "`n" | ForEach-Object {if($_ -match '^\s*\*?\s*([0-9a-fA-F]{1,16})\s'){$matches[1]}} | Select-Object -Unique)
    if($profiles.Count -eq 1){$null=& $exe switch $profiles[0] 2>&1;Start-Sleep -Seconds 3}
    $state=(& $exe status --json 2>$null | Out-String) | ConvertFrom-Json
}
if($state.BackendState -eq 'Running'){
    $null=& $exe set --unattended=true 2>&1
    if($LASTEXITCODE -ne 0){throw 'TailscaleRecoveryUnattendedFailed'}
}
Write-Output ('BRIDGE-NETWORK:'+([ordered]@{machine_hash=$machineHash;agent_id=[int]$cfg.agent_id;backend=$state.BackendState;ips=$state.TailscaleIPs;policy='always';needs_one_time_reauth=($state.BackendState -ne 'Running')} | ConvertTo-Json -Compress))
