"""Pure-logic checks - no audio/video hardware, no ffmpeg."""
import numpy as np

from app import (peak_level, select_segments, build_ffmpeg_cmd,
                 list_input_mics, list_dshow_audio, list_monitors, set_dpi_aware,
                 PRE, POST, SEG_LEN)


def test_peak_detector():
    silence = np.zeros(1024, dtype=np.float32)
    spike = silence.copy(); spike[500] = 0.9
    assert peak_level(silence) < 0.3
    assert peak_level(spike) >= 0.3
    assert peak_level(np.array([], dtype=np.float32)) == 0.0


def test_select_segments():
    T = 1000.0
    # one segment per second from T-30 to T+30
    segs = [("seg_%d.ts" % s, float(s)) for s in range(int(T - 30), int(T + 30))]
    chosen = select_segments(segs, T, PRE, POST, SEG_LEN)
    starts = [s for n, s in segs if n in chosen]
    # covers [T-15, T+15], allowing the 1s segment straddling each edge
    assert min(starts) <= T - PRE
    assert max(starts) <= T + POST
    assert max(starts) >= T + POST - SEG_LEN
    # nothing far outside the window
    assert all(T - PRE - SEG_LEN <= s <= T + POST for s in starts)
    # window length ~ pre+post seconds
    assert abs((max(starts) - min(starts)) - (PRE + POST)) <= 2 * SEG_LEN


def test_select_empty():
    assert select_segments([], 1000.0) == []


def test_build_ffmpeg_cmd():
    # 0 sources: video only, no amix
    c0 = build_ffmpeg_cmd([])
    assert "amix" not in " ".join(c0)
    assert c0.count("0:v") == 1
    # 1 source: direct map, no amix
    c1 = build_ffmpeg_cmd(["MicA"])
    assert "amix" not in " ".join(c1)
    assert c1.count("audio=MicA") == 1 and "1:a" in c1
    # 3 sources: amix=inputs=3 with three -i
    c3 = build_ffmpeg_cmd(["A", "B", "C"])
    j = " ".join(c3)
    assert "amix=inputs=3" in j
    assert j.count("-f dshow") == 3
    assert "[1:a][2:a][3:a]" in j
    # region crops gdigrab; no region = whole desktop
    assert "-video_size" not in " ".join(build_ffmpeg_cmd([]))
    jr = " ".join(build_ffmpeg_cmd([], region=(100, 200, 1920, 1080)))
    assert "-offset_x 100" in jr and "-offset_y 200" in jr and "1920x1080" in jr


def test_device_enumeration():
    # Don't assert non-empty (machine-dependent); just that they return lists
    # of the expected shape without throwing.
    mics = list_input_mics()
    assert isinstance(mics, list)
    assert all(isinstance(i, int) and isinstance(n, str) for i, n in mics)
    assert isinstance(list_dshow_audio(), list)
    set_dpi_aware()                          # must not throw on any platform
    mons = list_monitors()
    assert isinstance(mons, list)
    assert all(len(m) == 5 for m in mons)   # (x, y, w, h, is_primary)


if __name__ == "__main__":
    test_peak_detector()
    test_select_segments()
    test_select_empty()
    test_build_ffmpeg_cmd()
    test_device_enumeration()
    print("ok")
