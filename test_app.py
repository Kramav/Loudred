"""Pure-logic checks - no audio/video hardware, no ffmpeg."""
import io
import struct
import threading

import numpy as np

import app
from app import (rms_level, select_segments, select_range, build_ffmpeg_cmd,
                 estimate_footprint, list_input_mics, list_dshow_audio,
                 list_monitors, set_dpi_aware,
                 PRE, POST, SEG_LEN, MAX_EXTEND)


def test_level_detector():
    silence = np.zeros(1024, dtype=np.float32)
    assert rms_level(silence) == 0.0
    loud = np.full(1024, 0.5, dtype=np.float32)
    assert abs(rms_level(loud) - 0.5) < 1e-6
    # a single loud sample (a click) must NOT read as loud - the whole point of RMS
    click = silence.copy(); click[500] = 0.9
    assert rms_level(click) < 0.05
    assert rms_level(np.array([], dtype=np.float32)) == 0.0


def test_settings_roundtrip():
    import os
    orig = app.SETTINGS_FILE
    app.SETTINGS_FILE = os.path.join(os.path.dirname(orig), "loudred_settings.test.json")
    try:
        assert app.load_settings() == {}                 # missing file -> {}
        app.save_settings({"threshold": 0.12, "monitor": 2})
        got = app.load_settings()
        assert got["threshold"] == 0.12 and got["monitor"] == 2
    finally:
        try:
            os.remove(app.SETTINGS_FILE)
        except OSError:
            pass
        app.SETTINGS_FILE = orig


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


def test_select_range_merged_window():
    # 1s segments across a wide span
    segs = [("seg_%d.ts" % s, float(s)) for s in range(0, 200)]
    # a single peak at T == the usual PRE+POST window
    T = 100.0
    single = select_range(segs, T - PRE, T + POST)
    s_starts = [s for n, s in segs if n in single]
    assert min(s_starts) <= T - PRE and max(s_starts) >= T + POST - SEG_LEN
    # back-to-back peaks: first at T, last at T+10 -> one window covering both,
    # strictly longer than a single-peak clip (this is the #4 fix).
    first, last = T, T + 10.0
    merged = select_range(segs, first - PRE, last + POST)
    m_starts = [s for n, s in segs if n in merged]
    assert min(m_starts) <= first - PRE
    assert max(m_starts) >= last + POST - SEG_LEN
    assert (max(m_starts) - min(m_starts)) > (max(s_starts) - min(s_starts))
    # the extension is capped: clip never exceeds PRE + MAX_EXTEND + POST seconds
    capped_last = first + MAX_EXTEND
    capped = select_range(segs, first - PRE, capped_last + POST)
    c_starts = [s for n, s in segs if n in capped]
    assert (max(c_starts) - min(c_starts)) <= (PRE + MAX_EXTEND + POST + SEG_LEN)


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


def test_build_ffmpeg_scale():
    # native = no scale filter
    assert "scale=" not in " ".join(build_ffmpeg_cmd([]))
    # scaled, no audio: video goes through filter_complex, mapped [v]
    j = " ".join(build_ffmpeg_cmd([], scale_h=720))
    assert "scale=-2:720" in j and "-map [v]" in j
    # scale + 2 audio: one filtergraph holds BOTH the scale and the amix
    j2 = " ".join(build_ffmpeg_cmd(["A", "B"], scale_h=480))
    assert "scale=-2:480[v]" in j2 and "amix=inputs=2" in j2
    assert "-map [v]" in j2 and "-map [a]" in j2
    assert j2.count("-filter_complex") == 1     # combined, not two graphs
    # buffer dir is honored in the segment output path
    assert "ramdisk" in " ".join(build_ffmpeg_cmd([], bufdir="ramdisk"))


def test_build_ffmpeg_ram_and_encoder():
    # RAM mode -> fragmented mp4 to stdout, no segment muxer
    j = " ".join(build_ffmpeg_cmd([], ram=True))
    assert "pipe:1" in j and "frag_keyframe" in j and "-f segment" not in j
    # disk mode (default) still segments
    assert "-f segment" in " ".join(build_ffmpeg_cmd([]))
    # GPU encoder swaps the codec
    ja = " ".join(build_ffmpeg_cmd([], encoder="h264_amf"))
    assert "h264_amf" in ja and "libx264" not in ja
    assert "h264_nvenc" in " ".join(build_ffmpeg_cmd([], encoder="h264_nvenc"))


def test_build_ffmpeg_desktop_audio():
    # desktop audio only (no dshow): loopback PCM on stdin is the single audio map
    j = " ".join(build_ffmpeg_cmd([], desktop_audio=True))
    assert "-i pipe:0" in j and "f32le" in j
    assert "amix" not in j and "-map 1:a" in j        # pcm is input index 1
    # one dshow + desktop audio -> two sources mixed
    j2 = " ".join(build_ffmpeg_cmd(["MicA"], desktop_audio=True))
    assert "amix=inputs=2" in j2 and "[1:a][2:a]" in j2
    assert "audio=MicA" in j2 and "-i pipe:0" in j2
    # off by default: no stdin audio input
    assert "pipe:0" not in " ".join(build_ffmpeg_cmd(["MicA"]))


def _box(btype, payload=b""):
    return struct.pack(">I", 8 + len(payload)) + btype.encode("latin1") + payload


def test_iter_mp4_boxes():
    stream = _box("ftyp", b"isom") + _box("moov", b"x" * 20) + _box("moof", b"a")
    got = [(t, len(d)) for t, d in app.iter_mp4_boxes(io.BytesIO(stream).read)]
    assert [t for t, _ in got] == ["ftyp", "moov", "moof"]
    # a box split across reads still parses (size header may arrive in pieces)
    src = io.BytesIO(stream)
    assert sum(len(d) for _, d in app.iter_mp4_boxes(lambda n: src.read(3))) == len(stream)


def test_rambuffer_feed_and_slice():
    stream = (_box("ftyp", b"isom") + _box("moov", b"m" * 16)
              + _box("moof", b"a") + _box("mdat", b"AAAA")
              + _box("moof", b"b") + _box("mdat", b"BBBB"))
    rb = app.RamBuffer(retention=60)
    rb.feed_stream(io.BytesIO(stream).read, threading.Event())
    assert rb.init == _box("ftyp", b"isom") + _box("moov", b"m" * 16)
    assert len(rb.frags) == 2                       # two moof+mdat fragments
    assert rb.frags[0][1] == _box("moof", b"a") + _box("mdat", b"AAAA")

    # slice picks fragments by arrival time; prune drops old ones
    rb2 = app.RamBuffer(retention=60)
    rb2.init = b"INIT"
    rb2.frags.extend([(100.0, b"f1"), (105.0, b"f2"), (110.0, b"f3")])
    init, fr = rb2.slice(104, 111)
    assert init == b"INIT" and fr == [b"f2", b"f3"]
    rb2._prune(now=170.0)                            # cutoff 110 -> drops f1,f2
    assert [t for t, _ in rb2.frags] == [110.0]


def test_estimate_footprint():
    e = estimate_footprint(1920, 1080, 30, buffer_secs=60, clip_secs=30)
    # bitrate scales with pixels*fps*BPP; sanity-check order of magnitude
    assert 3 < e["mbps"] < 12
    # buffer holds 60s, clip holds 30s -> buffer is ~2x the clip
    assert abs(e["buffer_mb"] / e["clip_mb"] - 2.0) < 0.01
    # half the resolution -> ~quarter the bitrate
    half = estimate_footprint(960, 540, 30, 60, 30)
    assert abs(half["mbps"] / e["mbps"] - 0.25) < 0.01
    assert e["ram_mb"] > 150


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
    test_level_detector()
    test_settings_roundtrip()
    test_select_segments()
    test_select_empty()
    test_select_range_merged_window()
    test_build_ffmpeg_cmd()
    test_build_ffmpeg_scale()
    test_build_ffmpeg_ram_and_encoder()
    test_build_ffmpeg_desktop_audio()
    test_iter_mp4_boxes()
    test_rambuffer_feed_and_slice()
    test_estimate_footprint()
    test_device_enumeration()
    print("ok")
