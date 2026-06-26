<#
.SYNOPSIS
    Registers two scheduled tasks:
    1. "Reconnect Desk Soundbar" — polls every 2 min + fires on system wake
    2. "Desk Soundbar Keep-Alive" — plays silent audio at logon to prevent sleep
#>

$ScriptDir = $PSScriptRoot

# ============================================================
# Task 1: Reconnect (every 2 min + on wake from sleep/hibernate)
# ============================================================
$ReconnectTaskName = 'Reconnect Desk Soundbar'
$ReconnectScript   = Join-Path $ScriptDir 'Reconnect-DeskSoundbar.ps1'

Unregister-ScheduledTask -TaskName $ReconnectTaskName -Confirm:$false -ErrorAction SilentlyContinue

$reconnectVbs = Join-Path $ScriptDir 'Launch-Reconnect-Hidden.vbs'
$reconnectAction = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument "`"$reconnectVbs`""

# Trigger 1: every 2 minutes (repeating timer)
$timerTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Days 9999)

# Trigger 2: on workstation unlock (session unlock = display is back)
$unlockTrigger = New-ScheduledTaskTrigger -AtLogOn
# We'll use a CIM trigger for session unlock instead (event-based)
$unlockCim = Get-CimClass -ClassName MSFT_TaskSessionStateChangeTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$unlockTriggerObj = New-CimInstance -CimClass $unlockCim -ClientOnly -Property @{
    StateChange = 8  # 8 = Session Unlock
    UserId      = $env:USERDOMAIN + '\' + $env:USERNAME
}

$reconnectSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -WakeToRun

$reconnectPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited -LogonType Interactive

$reconnectTask = New-ScheduledTask `
    -Action $reconnectAction `
    -Trigger @($timerTrigger, $unlockTriggerObj) `
    -Settings $reconnectSettings `
    -Principal $reconnectPrincipal `
    -Description 'Reconnects Desk Soundbar and sets as default audio. Runs every 2 min + on session unlock.'

Register-ScheduledTask -TaskName $ReconnectTaskName -InputObject $reconnectTask

Write-Host "`nTask '$ReconnectTaskName' registered." -ForegroundColor Green
Write-Host '  Runs every 2 min + on session unlock.'

# ============================================================
# Task 2: Keep-Alive (silent audio loop at logon)
# ============================================================
$KeepAliveTaskName = 'Desk Soundbar Keep-Alive'
$KeepAliveScript   = Join-Path $ScriptDir 'Keep-Alive.ps1'

Unregister-ScheduledTask -TaskName $KeepAliveTaskName -Confirm:$false -ErrorAction SilentlyContinue

$vbsLauncher = Join-Path $ScriptDir 'Launch-Hidden.vbs'
$keepAliveAction = New-ScheduledTaskAction -Execute 'wscript.exe' `
    -Argument "`"$vbsLauncher`""

$keepAliveTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$keepAliveSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 999 `
    -MultipleInstances IgnoreNew

$keepAlivePrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Limited -LogonType Interactive

Register-ScheduledTask `
    -TaskName $KeepAliveTaskName `
    -Action $keepAliveAction `
    -Trigger $keepAliveTrigger `
    -Settings $keepAliveSettings `
    -Principal $keepAlivePrincipal `
    -Description 'Plays silent audio on loop to prevent the Desk Soundbar from sleeping due to inactivity.'

Write-Host "`nTask '$KeepAliveTaskName' registered." -ForegroundColor Green
Write-Host '  Starts at logon, plays silent audio continuously.'
Write-Host '  Restarts automatically if the process dies.'

Write-Host "`nLogs at:" -ForegroundColor Cyan
Write-Host "  $ScriptDir\logs\reconnect.log"
Write-Host "  $ScriptDir\logs\keepalive.log"
