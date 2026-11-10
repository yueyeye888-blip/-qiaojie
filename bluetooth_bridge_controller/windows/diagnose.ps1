$ErrorActionPreference='Stop'
$dir=Join-Path $env:ProgramData 'BluetoothBridge'
$cfgPath=Join-Path $dir 'agent.json'
$configured=Test-Path $cfgPath
$cfg=$null
if($configured){$cfg=Get-Content $cfgPath -Raw | ConvertFrom-Json}
$os=Get-CimInstance Win32_OperatingSystem
$machineGuid=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
$biosUuid=(Get-CimInstance Win32_ComputerSystemProduct).UUID
$sha=[Security.Cryptography.SHA256]::Create()
try{$machineHash=-join($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes("$machineGuid|$biosUuid")) | ForEach-Object {$_.ToString('x2')})}finally{$sha.Dispose()}
$task=Get-ScheduledTask -TaskName 'Mac Bluetooth Bridge Agent' -ErrorAction SilentlyContinue
$code=if(!$configured){'NotConfigured'}elseif(!$task){'AgentTaskMissing'}elseif($cfg.error){'AgentReportedFault'}else{'Configured'}
$result=[ordered]@{
    supported=([Environment]::Is64BitOperatingSystem -and $PSVersionTable.PSVersion.Major -ge 5)
    windows=$os.Caption
    powershell=[string]$PSVersionTable.PSVersion
    configured=[bool]$configured
    machine_hash=$machineHash
    controller=if($cfg){$cfg.poll_url}else{''}
    code=$code
    task=if($task){[string]$task.State}else{'Missing'}
    usb=if($cfg){$cfg.bluetooth_status}else{'unknown'}
}
Write-Output ('BRIDGE-DIAG:'+($result | ConvertTo-Json -Compress))
