[CmdletBinding()]
param(
    [string]$ConfigPath = "$env:ProgramData\BluetoothBridge\agent.json"
)

$ErrorActionPreference = 'Stop'
$agentVersion = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()

function Save-AgentConfig {
    param([hashtable]$Config)
    $tmp = "$ConfigPath.tmp"
    $Config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $ConfigPath -Force
}

function Invoke-VirtualHere {
    param([string]$Exe, [string]$Command)
    $resultPath = Join-Path $env:TEMP ("vh-result-" + [guid]::NewGuid().ToString('N') + '.txt')
    try {
        $process = Start-Process -FilePath $Exe -ArgumentList @('-t',('"'+$Command+'"'),'-r',('"'+$resultPath+'"')) -PassThru
        if (-not $process.WaitForExit(10000)) { throw 'VirtualHere command timed out' }
        if (Test-Path -LiteralPath $resultPath) {
            $result = [string](Get-Content -LiteralPath $resultPath -Raw)
            if ($result.Trim()) { return $result.Trim() }
        }
        throw 'VirtualHere returned no confirmation'
    }
    finally {
        Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue
    }
}

function Find-DeviceAddress {
    param([string]$ListText, [string]$UsbName)
    $addresses = @()
    foreach ($line in ($ListText -split "`r?`n")) {
        if ($line -match [regex]::Escape($UsbName) -and $line -match '\(([^()]+)\)') {
            $addresses += $Matches[1]
        }
    }
    if ($addresses.Count -gt 1) { throw 'Multiple matching USB adapters; refusing ambiguous selection' }
    if ($addresses.Count -eq 1) { return $addresses[0] }
    return ''
}

function Get-BluetoothStatus {
    param([string]$UsbVid, [string]$UsbPid)
    $pattern = "VID_$($UsbVid.ToUpper())&PID_$($UsbPid.ToUpper())"
    $device = Get-PnpDevice -PresentOnly -ErrorAction Stop | Where-Object {
        $_.InstanceId -like "*$pattern*"
    } | Select-Object -First 1
    if (-not $device) { return 'absent' }
    if ($device.Status -eq 'OK') { return 'ready' }
    return "pnp-$($device.Status)"
}

function Test-DeviceOwnedByUs {
    param([string]$ListText, [string]$Address)
    $lines=@($ListText -split "`r?`n" | Where-Object {
        $_.Contains('('+$Address+')') -and $_ -match '\(In[- ]use by you\)'
    })
    return $lines.Count -eq 1
}

function Confirm-UseLease {
    param([hashtable]$Config, [int]$Generation)
    # Driver enumeration can outlast one normal polling interval. Report only
    # preparation, never attachment, and honour revocation while waiting.
    $report=@{
        vh_status='preparing'; bluetooth_status=(Get-BluetoothStatus -UsbVid $Config.usb_vid -UsbPid $Config.usb_pid)
        applied_generation=-1; applied_action='unknown'; error=''; agent_version=$agentVersion
    } | ConvertTo-Json -Compress
    $response=Invoke-RestMethod -Method Post -Uri $Config.poll_url -Headers @{Authorization="Bearer $($Config.agent_token)"} -ContentType 'application/json' -Body $report -TimeoutSec 8
    if ($response.action -ne 'use' -or [int]$response.generation -ne $Generation -or [long]$response.lease_expires_at -le [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) {
        throw 'Lease changed or expired while preparing Bluetooth'
    }
}

function Invoke-AgentAction {
    param([hashtable]$Config, [string]$Action, [int]$Generation)
    $exe = $Config.virtualhere_exe
    if (-not (Test-Path -LiteralPath $exe)) { throw 'VirtualHere client executable is missing' }

    if ($Action -eq 'use') {
        if ((Get-BluetoothStatus -UsbVid $Config.usb_vid -UsbPid $Config.usb_pid) -ne 'absent') { throw 'Bluetooth device is already present before lease attachment' }
        Confirm-UseLease -Config $Config -Generation $Generation
        # The source denies idle connections. Recreate only this configured hub
        # on a new lease, instead of waiting on the client's old reconnect timer.
        $null = Invoke-VirtualHere -Exe $exe -Command "MANUAL HUB REMOVE,$($Config.hub)"
        $null = Invoke-VirtualHere -Exe $exe -Command "MANUAL HUB ADD,$($Config.hub)"
    }
    Start-Sleep -Seconds 2
    $list = Invoke-VirtualHere -Exe $exe -Command 'LIST'
    $address = Find-DeviceAddress -ListText $list -UsbName $Config.usb_name
    if ($Action -eq 'use' -and -not $address) {
        # A lease-gated source becomes reachable only after this request.
        $waitUntil = [DateTime]::UtcNow.AddSeconds(30)
        while (-not $address -and [DateTime]::UtcNow -lt $waitUntil) {
            Confirm-UseLease -Config $Config -Generation $Generation
            Start-Sleep -Seconds 2
            $list = Invoke-VirtualHere -Exe $exe -Command 'LIST'
            $address = Find-DeviceAddress -ListText $list -UsbName $Config.usb_name
        }
    }
    if ($Action -eq 'release' -and -not $address -and $Config.last_device_address) { $address = $Config.last_device_address }

    if ($Action -eq 'use') {
        if (-not $address) { throw "Target USB device '$($Config.usb_name)' is not visible" }
        if ((Get-BluetoothStatus -UsbVid $Config.usb_vid -UsbPid $Config.usb_pid) -ne 'absent') { throw 'Bluetooth device is already present before lease attachment' }
        Confirm-UseLease -Config $Config -Generation $Generation
        $result = Invoke-VirtualHere -Exe $exe -Command "USE,$address"
        if ($result -ne 'OK' -and $result -notmatch '^FAILED:\s*API Timeout \d+ sec$') { throw "VirtualHere USE did not confirm success: $result" }
        # An API response timeout is ambiguous. Never issue USE again: read back
        # exact-address local ownership and Windows PnP readiness under the lease.
        $Config.last_device_address = $address
        $readyUntil=[DateTime]::UtcNow.AddSeconds(30)
        $bluetoothState=Get-BluetoothStatus -UsbVid $Config.usb_vid -UsbPid $Config.usb_pid
        $owned=$false
        while (($bluetoothState -ne 'ready' -or -not $owned) -and [DateTime]::UtcNow -lt $readyUntil) {
            Confirm-UseLease -Config $Config -Generation $Generation
            $stateList=Invoke-VirtualHere -Exe $exe -Command 'LIST'
            $owned=Test-DeviceOwnedByUs -ListText $stateList -Address $address
            Start-Sleep -Seconds 2
            $bluetoothState=Get-BluetoothStatus -UsbVid $Config.usb_vid -UsbPid $Config.usb_pid
        }
        if (-not $owned) { throw 'VirtualHere local ownership was not confirmed' }
        if ($bluetoothState -ne 'ready') { throw "USB attached but Bluetooth device is not ready: $bluetoothState" }
        $Config.applied_generation = $Generation
        $Config.applied_action = 'use'
        $Config.vh_status = 'attached'
    }
    else {
        if ($address) {
            $result = Invoke-VirtualHere -Exe $exe -Command "STOP USING,$address"
            if ($result -match '^ERROR' -and $result -notmatch 'not.*using|not found|invalid address') {
                throw "VirtualHere STOP USING failed: $result"
            }
        }
        Start-Sleep -Seconds 2
        if ((Get-BluetoothStatus -UsbVid $Config.usb_vid -UsbPid $Config.usb_pid) -ne 'absent') { throw 'Bluetooth device is still present after release' }
        $Config.applied_generation = $Generation
        $Config.applied_action = 'release'
        $Config.vh_status = 'released'
    }
    $Config.bluetooth_status = Get-BluetoothStatus -UsbVid $Config.usb_vid -UsbPid $Config.usb_pid
    $Config.error = ''
    Save-AgentConfig -Config $Config
}

if (-not (Test-Path -LiteralPath $ConfigPath)) { throw "Agent config not found: $ConfigPath" }
$cfgObject = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$cfg = @{}
$cfgObject.PSObject.Properties | ForEach-Object { $cfg[$_.Name] = $_.Value }
if (-not $cfg.ContainsKey('blocked_generation')) { $cfg.blocked_generation = -1 }
if ($cfg.applied_action -eq 'use') { $cfg.blocked_generation = [int]$cfg.applied_generation }

# Never trust a persisted attached state after a process or machine restart.
$cfg.applied_action = 'unknown'
$cfg.applied_generation = -1
$cfg.vh_status = 'unknown'
$initialReleaseRequired = $true

while ($true) {
    $requestedUseGeneration = $null
    try {
        if ($initialReleaseRequired) {
            try {
                Invoke-AgentAction -Config $cfg -Action 'release' -Generation -1
                $initialReleaseRequired = $false
            } catch {
                $cfg.error = "Initial release failed: $($_.Exception.Message)"
                $cfg.vh_status = 'unknown'
            }
        }
        try { $cfg.bluetooth_status = Get-BluetoothStatus -UsbVid $cfg.usb_vid -UsbPid $cfg.usb_pid }
        catch { $cfg.bluetooth_status = 'unknown'; $cfg.error = $_.Exception.Message }
        if ($cfg.applied_action -eq 'use' -and $cfg.bluetooth_status -ne 'ready') { throw 'Bluetooth device became unavailable' }
        $report = @{
            vh_status = $cfg.vh_status
            device_address = $cfg.last_device_address
            bluetooth_status = $cfg.bluetooth_status
            applied_generation = $cfg.applied_generation
            applied_action = $cfg.applied_action
            error = $cfg.error
            agent_version = $agentVersion
        } | ConvertTo-Json -Compress
        $headers = @{ Authorization = "Bearer $($cfg.agent_token)" }
        $response = Invoke-RestMethod -Method Post -Uri $cfg.poll_url -Headers $headers -ContentType 'application/json' -Body $report -TimeoutSec 15
        $desiredAction = [string]$response.action
        $desiredGeneration = [int]$response.generation
        if ($initialReleaseRequired) { throw 'Initial USB release is not confirmed; refusing new actions' }
        if ($desiredAction -notin @('use','release')) { throw 'Invalid controller action' }
        if ($desiredAction -eq 'use' -and [long]$response.lease_expires_at -le [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) { throw 'Controller lease has expired' }
        if ($desiredAction -eq 'use') {
            $requestedUseGeneration = $desiredGeneration
            if ($desiredGeneration -eq [int]$cfg.blocked_generation) { throw 'Previous connection failed; start a new session from the controller' }
        }
        if ($desiredAction -ne $cfg.applied_action -or $desiredGeneration -ne [int]$cfg.applied_generation) {
            Invoke-AgentAction -Config $cfg -Action $desiredAction -Generation $desiredGeneration
        }
    }
    catch {
        $failure = $_.Exception.Message
        if ($null -ne $requestedUseGeneration) { $cfg.blocked_generation = $requestedUseGeneration }
        elseif ($cfg.applied_action -eq 'use') { $cfg.blocked_generation = [int]$cfg.applied_generation }
        $cfg.vh_status = 'unknown'
        $cfg.applied_action = 'unknown'
        # Fail closed on a lost controller connection, even before lease expiry.
        try { Invoke-AgentAction -Config $cfg -Action 'release' -Generation -1 }
        catch {
            # This service is dedicated to the bridge. Stopping it detaches USB
            # if its control API is unresponsive; require a successful release
            # check before reporting released or accepting another lease.
            $escapedExe = [regex]::Escape([string]$cfg.virtualhere_exe)
            Get-CimInstance Win32_Service | Where-Object { $_.PathName -match ('(?i)^"?' + $escapedExe + '(?:"|\s|$)') } | ForEach-Object { Stop-Service -Name $_.Name -Force -ErrorAction SilentlyContinue }
            $initialReleaseRequired = $true
        }
        $cfg.error = $failure
        try { Save-AgentConfig -Config $cfg } catch {}
    }
    Start-Sleep -Seconds ([Math]::Max(3, [int]$cfg.poll_seconds))
}

