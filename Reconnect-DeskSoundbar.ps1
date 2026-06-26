<#
.SYNOPSIS
    Monitors and reconnects the Desk Soundbar Bluetooth speaker,
    then sets it as the default audio output device.

.DESCRIPTION
    - Checks if "Desk Soundbar" audio endpoint is active
    - If disconnected, uses pnputil to toggle the BT device (no admin needed)
    - Sets "Speakers (Desk Soundbar)" as the default audio output
    - Logs actions to a timestamped log file

.NOTES
    Schedule via Task Scheduler for hands-free operation.
#>

param(
    [switch]$Silent
)

$ErrorActionPreference = 'Stop'

# --- Configuration ---
$SpeakerName       = 'Desk Soundbar'
$BtInstanceId      = 'BTHENUM\DEV_5415896DE33F\7&3F54149&0&BLUETOOTHDEVICE_5415896DE33F'
$LogFile           = Join-Path $PSScriptRoot 'logs\reconnect.log'
$MaxRetries        = 3
$RetryDelaySec     = 5

# --- Helpers ---
function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    $line = "$ts  $Message"
    if (-not $Silent) { Write-Host $line }
    Add-Content -Path $LogFile -Value $line -ErrorAction SilentlyContinue
}

function Set-DefaultAudioDevice {
    param([string]$DeviceId)

    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

[Guid("F8679F50-850A-41CF-9C72-430F290290C8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface IPolicyConfig {
    int unused1(); int unused2(); int unused3(); int unused4();
    int unused5(); int unused6(); int unused7(); int unused8();
    int unused9(); int unused10();
    [PreserveSig] int SetDefaultEndpoint([MarshalAs(UnmanagedType.LPWStr)] string deviceId, int role);
}

[ComImport, Guid("870AF99C-171D-4F9E-AF0D-E63DF40C2BC9")]
internal class PolicyConfigClient {}

public static class AudioSwitcher {
    public static void SetDefault(string deviceId) {
        var config = (IPolicyConfig)new PolicyConfigClient();
        Marshal.ThrowExceptionForHR(config.SetDefaultEndpoint(deviceId, 0));
        Marshal.ThrowExceptionForHR(config.SetDefaultEndpoint(deviceId, 1));
    }
}
"@ -ErrorAction SilentlyContinue

    [AudioSwitcher]::SetDefault($DeviceId)
}

# --- Ensure log directory ---
$logDir = Split-Path $LogFile
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

# --- Main ---
Write-Log '--- Reconnect check started ---'

# 1. Check if the Bluetooth device is paired
$btDevice = Get-PnpDevice -InstanceId $BtInstanceId -ErrorAction SilentlyContinue

if (-not $btDevice) {
    Write-Log "ERROR: '$SpeakerName' not found as a paired Bluetooth device."
    exit 1
}

# 2. Check if the audio endpoint is active
$audioEndpoint = Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
                 Where-Object { $_.FriendlyName -like "*$SpeakerName*" -and $_.Status -eq 'OK' }

if ($audioEndpoint) {
    Write-Log "'$SpeakerName' is already connected (audio endpoint active)."
} else {
    Write-Log "'$SpeakerName' audio endpoint NOT found. Attempting reconnect..."

    for ($i = 1; $i -le $MaxRetries; $i++) {
        Write-Log "  Attempt $i/$MaxRetries - toggling Bluetooth device..."

        # Use pnputil to restart the device (works without elevation on paired BT devices)
        & pnputil /restart-device $BtInstanceId 2>&1 | Out-Null
        Start-Sleep -Seconds $RetryDelaySec

        # If pnputil didn't work, try disable/enable (needs admin, but try anyway)
        $audioEndpoint = Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
                         Where-Object { $_.FriendlyName -like "*$SpeakerName*" -and $_.Status -eq 'OK' }

        if (-not $audioEndpoint) {
            Disable-PnpDevice -InstanceId $BtInstanceId -Confirm:$false -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            Enable-PnpDevice -InstanceId $BtInstanceId -Confirm:$false -ErrorAction SilentlyContinue
            Start-Sleep -Seconds $RetryDelaySec

            $audioEndpoint = Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
                             Where-Object { $_.FriendlyName -like "*$SpeakerName*" -and $_.Status -eq 'OK' }
        }

        if ($audioEndpoint) {
            Write-Log "  Reconnected on attempt $i."
            break
        }
    }

    if (-not $audioEndpoint) {
        Write-Log "FAILED: Could not reconnect '$SpeakerName' after $MaxRetries attempts."
        exit 2
    }
}

# 3. Set as default audio output
try {
    $mmId = $audioEndpoint.InstanceId -replace '^SWD\\MMDEVAPI\\', ''
    Set-DefaultAudioDevice -DeviceId $mmId
    Write-Log "Set '$($audioEndpoint.FriendlyName)' as default audio device."
} catch {
    Write-Log "WARNING: Could not set default device: $_"
}

Write-Log '--- Reconnect check complete ---'
exit 0
