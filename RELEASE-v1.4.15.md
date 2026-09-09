# JoyHub MaxSensr Funscript Player v1.4.15

## Persistent Resume Update

Version 1.4.15 improves playlist handling and playback persistence while retaining the BLE, funscript and bilingual interface improvements from previous releases.

### What's new

- Complete-folder playlist loading
- Remembers the last selected folder
- Remembers and restores the last selected video at startup
- Optional resume from the last playback position
- Independent saved resume position for each video
- Playback position saved periodically and when playback stops
- Saved position removed when a video finishes normally
- Improved atomic configuration saving
- Improved funscript discovery:
  - local `Funscript` subfolder
  - adjacent `.funscript`
  - global Funscript directory

### Retained improvements

- Robust loader for standard funscript JSON and concatenated JSON blocks
- French / English interface
- Direct BLE control of JoyHub J-MaxSensr
- MPV synchronized playback
- OSC, VIB and CON controls
- Live control adjustments
- Ultra-slow OSC pulse mode
- Interactive timeline and seek support

## Main file

`MaxSensr-Funscript-Player-v1.4.15-PersistentResume.py`

## Requirements

Install the Python dependency with:

```bash
pip install -r requirements.txt
```

System requirements include Python 3, Tkinter, MPV and a Bluetooth Low Energy adapter.
