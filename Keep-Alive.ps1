<#
.SYNOPSIS
    Plays silent audio on a loop to keep the Bluetooth soundbar awake.

.DESCRIPTION
    Generates a short silent WAV file and plays it on repeat.
    This keeps the Bluetooth A2DP audio stream active, preventing
    the soundbar from entering sleep mode due to inactivity.

    Runs indefinitely — designed to be launched at logon via Task Scheduler.
#>

param(
    [int]$IntervalSeconds = 25
)

$wavPath = Join-Path $PSScriptRoot 'silence.wav'
$logFile = Join-Path $PSScriptRoot 'logs\keepalive.log'
$logDir  = Split-Path $logFile
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $logFile -Value "$ts  $Message" -ErrorAction SilentlyContinue
}

# --- Generate a 10-second silent WAV file (8kHz, 8-bit mono, ~80KB) ---
if (-not (Test-Path $wavPath)) {
    Write-Log 'Generating silence.wav...'

    $sampleRate  = 8000
    $duration    = 10
    $numSamples  = $sampleRate * $duration
    $dataSize    = $numSamples
    $fileSize    = $dataSize + 36

    $ms = New-Object System.IO.MemoryStream
    $bw = New-Object System.IO.BinaryWriter($ms)

    # RIFF header
    $bw.Write([byte[]][char[]]'RIFF')
    $bw.Write([int]$fileSize)
    $bw.Write([byte[]][char[]]'WAVE')

    # fmt chunk
    $bw.Write([byte[]][char[]]'fmt ')
    $bw.Write([int]16)          # chunk size
    $bw.Write([int16]1)         # PCM format
    $bw.Write([int16]1)         # mono
    $bw.Write([int]$sampleRate) # sample rate
    $bw.Write([int]$sampleRate) # byte rate (sampleRate * channels * bitsPerSample/8)
    $bw.Write([int16]1)         # block align
    $bw.Write([int16]8)         # bits per sample

    # data chunk
    $bw.Write([byte[]][char[]]'data')
    $bw.Write([int]$dataSize)
    # 0x80 = silence for unsigned 8-bit PCM
    $silence = [byte[]]::new($numSamples)
    for ($i = 0; $i -lt $numSamples; $i++) { $silence[$i] = 0x80 }
    $bw.Write($silence)

    $bw.Flush()
    [System.IO.File]::WriteAllBytes($wavPath, $ms.ToArray())
    $bw.Dispose()
    $ms.Dispose()

    Write-Log 'silence.wav created.'
}

# --- Play silence on repeat ---
Write-Log "Keep-alive started (interval: ${IntervalSeconds}s)"

$player = New-Object System.Media.SoundPlayer
$player.SoundLocation = $wavPath
$player.Load()

while ($true) {
    try {
        $player.PlaySync()
    } catch {
        Write-Log "Playback error: $_"
    }
    Start-Sleep -Seconds $IntervalSeconds
}
