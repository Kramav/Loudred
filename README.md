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
- Pick a **Capture quality** — Native, or downscale to 1080p/720p/480p to shrink
  CPU load and buffer size.
- Set the **Clip window** — how many seconds before and after a peak to keep.
- Optionally point the **Buffer folder** at a RAM disk (see *Sparing your SSD*).
- Tick **Stats for nerds** to see the estimated bitrate, rolling-buffer size,
  per-clip size and ffmpeg RAM for your current settings.
- Pick your **Trigger mic** (the mic whose loudness arms a clip).
- Select one or more **audio sources to record** (multi-select with Ctrl/Shift) —
  these get mixed into the clip. Your mic + a loopback are pre-selected.
- Move the **threshold** slider while watching the **live meter**: the bar turns
  **red / "WOULD CLIP"** when the level crosses the threshold (white marker line),
  green / "armed" otherwise.
- Tick **Test mode** to watch levels without ever saving a clip.
- Click **Start**. When your mic gets loud above the threshold, a clip is written
  to the clips folder. **Change…** moves that folder; **Open clips folder** opens it.

All of these (monitor, quality, clip window, buffer folder, mic, sources,
threshold, clips folder, stats toggle) are remembered between runs in
`loudred_settings.json` next to `app.py`.

## Sparing your SSD (RAM buffer)

The rolling buffer writes a new ~1-second video segment every second, forever
while recording — that's a lot of continuous writes on an SSD. To put the buffer
in RAM instead, create a RAM disk and point the **Buffer folder** at it:

1. Install a RAM-disk tool (e.g. the free **ImDisk Toolkit** or **OSFMount**).
2. Create a small RAM disk — a few hundred MB is plenty (the *Stats for nerds*
   readout shows how much the buffer needs for your settings). Give it a drive
   letter, e.g. `R:`.
3. Set **Buffer folder** to something like `R:\loudred` and Start.

Saved clips still go to your normal (disk) clips folder — only the short-lived
rolling buffer lives in RAM, so nothing is lost if the RAM disk clears on reboot.

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

Clip window (seconds before/after) and capture quality are now GUI settings;
their defaults (`PRE`, `POST`, `QUALITY_PRESETS`) and the other knobs (`FPS`,
`SEG_LEN`, `BITS_PER_PIXEL` for the estimate) live at the top of `app.py`. The
buffer length adapts automatically to the clip window (`pre + post + MAX_EXTEND`
plus a margin).

## Test

```
python test_app.py
```

Checks the segment-selection, clip-merge, loudness and settings logic
(no hardware needed).

## Notes / limits

- Clips are aligned to 1-second segment boundaries (~±1s); exact enough for replay.
- If your mic stays loud (a long laugh, a rally), the clip **extends** to keep
  covering it — up to `PRE + MAX_EXTEND + POST` seconds — instead of cutting off
  or dropping the follow-up moment.
- Single-monitor screen capture, no webcam overlay, no .exe packaging — ask if you want these.
