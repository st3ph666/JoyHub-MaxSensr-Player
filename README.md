# JoyHub MaxSensr Funscript Player

Linux desktop player for the **JoyHub J-MaxSensr** with direct Bluetooth Low Energy control and synchronized `.funscript` playback.

The interface is available in **English and French** and can switch language directly from the application.

## Features

- Direct BLE connection to `J-MaxSensr`
- Synchronized video playback with MPV
- Automatic `.funscript` detection
- Complete folder playlist support
- Remembers the last selected folder and video
- Optional resume from the last playback position
- Persistent resume position stored independently for each video
- Automatic cleanup of the saved position when a video finishes normally
- Oscillation (OSC), vibration (VIB), and constriction (CON) controls
- Independent enable/disable controls for OSC, VIB, and CON
- Live control adjustments during playback
- Ultra-slow OSC pulse mode
  - Pulse ON time
  - Pause OFF time
  - Pulse force
- OSC min/max, amplitude, and transition controls
- Vibration patterns and smoothing controls
- Constriction patterns, threshold, level, and pump duration
- Interactive full-timeline funscript graph
- Seek support with BLE reconnect handling
- Automatic next-video option
- Optional delete-after-playback
- French / English interface
- Tolerant funscript loader for standard JSON and concatenated JSON blocks
- Atomic persistent configuration saving

## Screenshots

### Français

![JoyHub MaxSensr Player - Français](screenshots/maxsensr-fr.png)

### English

![JoyHub MaxSensr Player - English](screenshots/maxsensr-en.png)

## Requirements

- Linux
- Python 3
- Tkinter
- MPV
- Python package `bleak`
- Bluetooth Low Energy adapter
- JoyHub `J-MaxSensr`

Example Debian/Ubuntu packages:

```bash
sudo apt install python3 python3-tk mpv python3-venv
```

Create a virtual environment and install the Python requirements:

```bash
python3 -m venv ~/venv-maxsensr
~/venv-maxsensr/bin/pip install -r requirements.txt
```

## Run

```bash
~/venv-maxsensr/bin/python MaxSensr-Funscript-Player-v1.4.16-PersistentResume.py
```

You can select a single video or load a complete folder as a playlist. The player searches for the matching `.funscript`, connects to the MaxSensr over BLE, and synchronizes the enabled functions with playback.

The player can remember the last selected video and its playback position. When **Resume video** is enabled, reopening the application restores the last video and resumes from its saved position.

## Funscript lookup

For each video, the player checks for the matching funscript in this order:

1. A `Funscript` subfolder beside the video
2. A `.funscript` file beside the video
3. The configured global `Funscript` directory

## Ultra-slow oscillation

The **Ultra-slow pulse mode** reduces the effective oscillation speed by driving OSC intermittently instead of continuously. The three live controls are:

- **Pulse ON** — how long the OSC motor is driven
- **Pause OFF** — how long it rests between pulses
- **Pulse force** — strength applied during each pulse

These controls can be adjusted while the video is playing.

## Safety

Start with conservative levels and verify the device response before increasing force or duration. Stop playback if the device behaves unexpectedly, overheats, stalls, or loses synchronization.

## Version

**v1.4.16 — Persistent Resume**

Highlights of this release:

- Added complete-folder playlist loading
- Remembers the last folder and last selected video
- Restores the last video when the application starts
- Added optional per-video playback resume
- Saves playback positions periodically and when playback stops
- Improved persistent and atomic configuration saving
- Improved funscript discovery in local `Funscript` subfolders
- Retains the robust concatenated-JSON funscript loader from v1.4.13
- Retains French / English interface switching
- Retains live OSC, VIB and CON controls
- Retains Ultra-slow OSC pulse controls

## License

No license has been selected yet. Add a `LICENSE` file before distributing the project under a specific open-source license.

## uv deployment

The recommended deployment method is [`uv`](https://docs.astral.sh/uv/). The project is configured to use the system Python so Tkinter remains provided by the Linux distribution.

### Debian / Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-tk mpv bluetooth bluez
```

Install `uv` using its official installation method, then:

```bash
git clone https://github.com/st3ph666/JoyHub-MaxSensr-Player.git
cd JoyHub-MaxSensr-Player
uv sync
uv run python MaxSensr-Funscript-Player-v1.4.16-PersistentResume.py
```

`bleak` is installed automatically by `uv sync`. Do not run `uv sync` with `sudo`.

### Update

```bash
git pull
uv sync
uv run python MaxSensr-Funscript-Player-v1.4.16-PersistentResume.py
```

## Source architecture

```text
MaxSensr-Funscript-Player-v1.4.16-PersistentResume.py  # Compatibility launcher
src/maxsensr_player/
├── __init__.py                                      # Version metadata
├── settings.py                                      # Device, path and profile constants
├── app.py                                           # BLE worker, Funscript helpers and Tkinter application
└── main.py                                          # Application entry point
```

Source-code comments are maintained in **English only**. French / English user-interface text is preserved.

