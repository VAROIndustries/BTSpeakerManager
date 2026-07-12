# BTSpeakerManager

_Built by [VARØ Industries](https://varo.industries/apps)_

A Windows system tray app that keeps Bluetooth speakers alive and connected.

Bluetooth speakers go to sleep when there's no audio. Windows doesn't reconnect them or switch back when they wake up. This app fixes that.

## What It Does

- **Silent keep-alive** -- plays an inaudible tone so the speaker never enters standby
- **Auto default device** -- sets your BT speaker as the default audio output when it connects
- **Fallback audio** -- switches to wired output (e.g. Realtek) when the speaker disconnects
- **System tray** -- runs quietly with color-coded icons (blue = connected, gray = disconnected, orange = paused)
- **Speaker selection** -- pick from detected Bluetooth audio endpoints
- **Configurable polling** -- 30s, 1m, 2m, 5m, or 10m intervals
- **No audio blips** -- only changes the default device on state transitions, not every poll cycle
- **Auto-start** -- optional Windows startup via registry

## Install

### Standalone EXE (recommended)

Download `BTSpeakerManager.exe` from [Releases](https://github.com/VAROIndustries/BTSpeakerManager/releases) and run it. No Python needed.

### From Source

```
pip install pystray Pillow pycaw comtypes
python bt_tray.py
```

### Build the EXE

```
pip install pyinstaller
build.bat
```

Output: `dist/BTSpeakerManager.exe` (~27MB standalone)

## Configuration

All settings are stored in `config.json` next to the executable. Configure everything from the tray menu:

- Speaker selection
- Polling interval
- Enable/disable polling
- Enable/disable keep-alive
- Auto-start with Windows

## How It Works

BTSpeakerManager polls Windows audio endpoints via [pycaw](https://github.com/AndreMiras/pycaw) and tracks your selected Bluetooth speaker's connection state. On state change:

| Event | Action |
|---|---|
| Speaker connects | Set as default audio device, start silent keep-alive |
| Speaker disconnects | Stop keep-alive, switch to fallback device |
| No change | Do nothing |

The keep-alive is a WAV file containing a single sample of silence, played on continuous loop via `winsound.SND_LOOP`. The speaker sees an active audio stream and stays awake.

## Requirements

- Windows 10/11
- Python 3.10+ (if running from source)

## License

MIT
