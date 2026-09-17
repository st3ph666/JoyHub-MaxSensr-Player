# JoyHub MaxSensr Funscript Player

Linux desktop player for the **JoyHub J-MaxSensr** with direct Bluetooth Low Energy control and synchronized `.funscript` playback.

## Current version

**v1.5.1 — Playlist Transition Fix**

Main script:

`MaxSensr-Funscript-Player-v1.5.1-PlaylistTransitionFix.py`

## Features

- Direct BLE connection to `J-MaxSensr`
- Synchronized video playback with MPV
- Automatic `.funscript` detection
- Complete folder playlist support
- Automatic next-video playback
- Live OSC, VIB and CON controls
- Ultra-slow OSC pulse mode with ON time, OFF pause and pulse force
- Vibration and constriction patterns
- Interactive full-timeline funscript graph
- Seek support with BLE reconnect handling
- Optional delete-after-playback
- Persistent live configuration

## v1.5.1 changes

This release focuses on reliable transitions between playlist videos and Bluetooth stability.

- Fixed a race condition when automatically starting the next video
- Each playback session now receives its own stop event
- Prevents an old BLE worker from restarting after a new video begins
- Waits for the previous playback/BLE thread to finish before opening the next session
- Prevents two concurrent BLE sessions from competing for the MaxSensr
- Improved transition timing so BlueZ and the MaxSensr have time to release the previous connection
- Keeps the UI responsive while waiting for the previous session to terminate
- Retains folder playlist and live configuration features introduced in v1.5.0

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
sudo apt install python3 python3-tk mpv python3-venv bluetooth bluez
```

Create a virtual environment and install the Python requirements:

```bash
python3 -m venv ~/venv-maxsensr
~/venv-maxsensr/bin/pip install -r requirements.txt
```

## Run

```bash
~/venv-maxsensr/bin/python MaxSensr-Funscript-Player-v1.5.1-PlaylistTransitionFix.py
```

You can select a single video or load a complete folder as a playlist. The player searches for the matching `.funscript`, connects to the MaxSensr over BLE, and synchronizes the enabled functions with playback.

## Playlist transition handling

v1.5.1 changes the lifecycle of the BLE worker. When playback changes to another video, the previous session is stopped permanently before the new session is allowed to connect. A new stop event is allocated for every playback session, preventing an older worker from seeing its stop state cleared and reconnecting at the same time as the new worker.

This specifically addresses repeated connect/disconnect cycles and difficulty starting the next video in a folder playlist.

## Funscript lookup

The player searches for the matching `.funscript` associated with the selected video and synchronizes the script timeline with MPV playback.

## Ultra-slow oscillation

The **Ultra-slow pulse mode** reduces the effective oscillation speed by driving OSC intermittently instead of continuously. The live controls include pulse ON time, pause OFF time and pulse force.

## Safety

Start with conservative levels and verify the device response before increasing force or duration. Stop playback if the device behaves unexpectedly, overheats, stalls, or loses synchronization.

## Version history

### v1.5.1 — Playlist Transition Fix

- BLE worker lifecycle fix
- Reliable automatic next-video transition
- Protection against concurrent BLE sessions
- Improved BlueZ/MaxSensr connection release timing

### v1.5.0 — Folder Playlist / Live Config

- Complete-folder playlist playback
- Automatic next-video option
- Live configuration updates during playback
- Persistent live settings
- Interactive funscript timeline and seek handling

### v1.4.x

- Direct BLE MaxSensr control
- MPV synchronization
- OSC/VIB/CON control
- Pattern controls
- Ultra-slow OSC pulse mode
- Persistent playback/configuration improvements

## License

No license has been selected yet. Add a `LICENSE` file before distributing the project under a specific open-source license.

## uv deployment

The project can also be deployed with `uv` while keeping Tkinter provided by the Linux distribution.

```bash
git clone https://github.com/st3ph666/JoyHub-MaxSensr-Player.git
cd JoyHub-MaxSensr-Player
uv sync
uv run python MaxSensr-Funscript-Player-v1.5.1-PlaylistTransitionFix.py
```

To update:

```bash
git pull
uv sync
uv run python MaxSensr-Funscript-Player-v1.5.1-PlaylistTransitionFix.py
```
