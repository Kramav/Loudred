# Loudred — peak-triggered screen + audio clipper

Shadowplay-style instant replay. It continuously buffers your **screen video +
desktop audio (incl. Discord) + microphone**, and saves a 30-second clip
(**15s before + 15s after**) **only when your mic gets loud** (a shout, clap,
laugh — whatever crosses the threshold).

## Setup (one time)

1. **Install ffmpeg** and make sure it's on your `PATH`
   (`ffmpeg -version` should work in a terminal).
2. **Install Python deps:** `pip install -r requirements.txt`
3. **Enable a loopback audio device** so desktop + Discord audio can be captured
   (ffmpeg can't tap Windows output directly):
   - **Easiest:** enable **Stereo Mix** — Sound settings → More sound settings →
     Recording tab → right-click → Show Disabled Devices → enable *Stereo Mix*.
   - **If you have no Stereo Mix:** install the free
     [VB-Audio Virtual Cable](https://vb-audio.com/Cable/), set it as an
     *additional* playback path, or use "Listen to this device" to route output
     through it. The app auto-detects `CABLE Output`.
   - No loopback? The app still runs — clips just contain **mic audio only**.

## Run

```
python app.py
```

- Pick which **screen/monitor** to record (defaults to your **primary** monitor;
  "All monitors" captures the whole desktop). The clip is screen video (H.264).
- Pick your **Trigger mic** (the mic whose loudness arms a clip).
- Select one or more **audio sources to record** (multi-select with Ctrl/Shift) —
  these get mixed into the clip. Your mic + a loopback are pre-selected.
- Move the **threshold** slider while watching the **live meter**: the bar turns
  **red / "WOULD CLIP"** when the level crosses the threshold (white marker line),
  green / "armed" otherwise.
- Tick **Test mode** to watch levels without ever saving a clip.
- Click **Start**. When your mic gets loud above the threshold, a clip is written
  to the clips folder. **Change…** moves that folder; **Open clips folder** opens it.

Your monitor / mic / sources / threshold / folder are remembered between runs
(in `loudred_settings.json` next to `app.py`).

List detected audio devices:

```
python app.py --list-devices
```

Override auto-detection if needed:

```
python app.py --mic "Microphone (Realtek)" --loopback "Stereo Mix (Realtek)"
```

## Tuning

If it triggers too easily, raise the threshold; if it never fires, lower it —
use the live meter to see where your normal vs. loud level sits. The level is
**RMS loudness**, not raw peaks, so brief clicks/pops won't trip it (default
threshold `0.1`).

Other knobs live at the top of `app.py`: `PRE`, `POST` (clip window), `FPS`,
`SEG_LEN`, `RETENTION` (buffer length).

## Test

```
python test_app.py
```

Checks the segment-selection and peak-detection logic (no hardware needed).

## Notes / limits

- Clips are aligned to 1-second segment boundaries (~±1s); exact enough for replay.
- Single-monitor screen capture, no webcam overlay, no .exe packaging — ask if you want these.
