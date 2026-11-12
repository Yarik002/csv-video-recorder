"""Tests for apply_zoom.py (Internship task 2).

The zoom math and the sidecar parsing need no ffmpeg. The render tests make a
small synthetic video with a red square and check where the square ends up in
the zoomed frames; they are skipped when ffmpeg is not installed (CI installs it).

Run: python -m pytest tests/test_zoom.py -v
"""

import json
import os
import shutil
import subprocess

import pytest

import apply_zoom

VIEWPORT = {"width": 1280, "height": 720}
SIZE = (1280, 720)


def config(events, **kwargs):
    data = {"viewport": VIEWPORT, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5, "events": events}
    data.update(kwargs)
    return apply_zoom.parse_sidecar(data)


def ev(t, kind, x=640, y=360):
    return {"t": t, "x": x, "y": y, "type": kind}


def pairs_of(events, ramp=0.5):
    return apply_zoom.pair_events(apply_zoom.parse_sidecar({"events": events})["events"], ramp)


# ---------- sidecar ----------

def test_sidecar_path():
    assert apply_zoom.sidecar_path(os.path.join("video", "ACME_GmbH.webm")) == \
        os.path.join("video", "ACME_GmbH.zoom.json")


def test_parse_example_from_the_task():
    cfg = config([ev(3.0, "in"), ev(8.0, "out")])
    assert cfg["viewport"] == VIEWPORT
    assert (cfg["fps"], cfg["zoom_factor"], cfg["ramp_sec"]) == (25, 2.0, 0.5)
    assert [e["type"] for e in cfg["events"]] == ["in", "out"]


def test_defaults_and_sorting():
    cfg = apply_zoom.parse_sidecar({"events": [ev(5, "out"), ev(1, "in")]})
    assert cfg["viewport"] == VIEWPORT and cfg["fps"] == 25
    assert [e["t"] for e in cfg["events"]] == [1, 5]


def test_old_sidecar_without_type_toggles():
    cfg = apply_zoom.parse_sidecar({"events": [{"t": 1, "x": 1, "y": 2}, {"t": 2, "x": 3, "y": 4},
                                               {"t": 3, "x": 5, "y": 6}]})
    assert [e["type"] for e in cfg["events"]] == ["in", "out", "in"]


@pytest.mark.parametrize("data, message", [
    ([], "JSON object"),
    ({"events": {}}, "events must be a list"),
    ({"events": [{"t": "1", "x": 0, "y": 0}]}, "events[0].t"),
    ({"events": [{"t": -1, "x": 0, "y": 0}]}, ">= 0"),
    ({"events": [{"t": 1, "x": 0, "y": 0, "type": "zoom"}]}, "'in' or 'out'"),
    ({"zoom_factor": 0.5}, "zoom_factor"),
    ({"fps": 0}, "fps"),
    ({"viewport": {"width": 0, "height": 720}}, "viewport.width"),
])
def test_bad_sidecar(data, message):
    with pytest.raises(apply_zoom.SidecarError, match=message.replace("[", r"\[").replace("]", r"\]")):
        apply_zoom.parse_sidecar(data)


def test_bad_json_file(tmp_path):
    path = tmp_path / "v.zoom.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(apply_zoom.SidecarError, match="not valid JSON"):
        apply_zoom.load_sidecar(str(path))


# ---------- pairing ----------

def test_pairs():
    pairs, warnings = pairs_of([ev(3, "in", 100, 200), ev(8, "out", 500, 500)])
    assert pairs == [(3, 100, 200, 8)]  # the point of the "in" is the zoom centre
    assert warnings == []


def test_multiple_pairs():
    pairs, _ = pairs_of([ev(1, "in", 10, 10), ev(2, "out"), ev(4, "in", 20, 20), ev(6, "out")])
    assert pairs == [(1, 10, 10, 2), (4, 20, 20, 6)]


def test_open_ended_zoom_holds_to_the_end():
    pairs, _ = pairs_of([ev(1, "in"), ev(2, "out"), ev(5, "in", 300, 300)])
    assert pairs[-1] == (5, 300, 300, None)
    assert apply_zoom.zoom_level(5.5, pairs, 0.5) == 1.0
    assert apply_zoom.zoom_level(10_000, pairs, 0.5) == 1.0


def test_stray_events_are_ignored():
    pairs, warnings = pairs_of([ev(1, "out"), ev(2, "in"), ev(3, "in"), ev(4, "out"), ev(5, "out")])
    assert pairs == [(2, 640, 360, 4)]
    assert len(warnings) == 3


def test_overlapping_pair_is_moved_after_the_previous_one():
    # The second zoom starts 0.2 s after the first zoom-out began, the ramp is 0.5 s.
    pairs, warnings = pairs_of([ev(1, "in"), ev(2, "out"), ev(2.2, "in"), ev(5, "out")])
    assert pairs == [(1, 640, 360, 2), (2.5, 640, 360, 5)]
    assert "moved to 2.5" in warnings[0]


def test_pair_with_nothing_left_is_dropped():
    pairs, warnings = pairs_of([ev(1, "in"), ev(2, "out"), ev(2.1, "in"), ev(2.3, "out")])
    assert pairs == [(1, 640, 360, 2)]
    assert "dropped" in warnings[-1]


# ---------- ramp, easing, crop ----------

def test_ease():
    assert apply_zoom.ease(0) == 0 and apply_zoom.ease(1) == 1 and apply_zoom.ease(0.5) == 0.5
    assert apply_zoom.ease(-1) == 0 and apply_zoom.ease(2) == 1
    # slow at both ends, faster in the middle: not a linear ramp
    assert apply_zoom.ease(0.1) < 0.1 and apply_zoom.ease(0.9) > 0.9


def test_ramp_frames():
    """25 fps, ramp 0.5 s: frames every 0.04 s. The zoom-in starts at the "in" event,
    takes 0.5 s, holds, the zoom-out starts at the "out" event and takes 0.5 s too."""
    cfg = config([ev(3.0, "in"), ev(8.0, "out")])
    pairs, _ = apply_zoom.pair_events(cfg["events"], cfg["ramp_sec"])
    zooms = [apply_zoom.frame_at(n / 25, pairs, cfg, SIZE)[0] for n in range(0, 10 * 25)]
    changing = [n for n in range(1, len(zooms)) if abs(zooms[n] - zooms[n - 1]) > 1e-9]
    assert changing == list(range(76, 89)) + list(range(201, 214))  # 3.04..3.52 s, 8.04..8.52 s
    assert zooms[74] == 1.0 and zooms[75] == 1.0           # 3.00 s: the ramp starts here
    assert zooms[88] == 2.0 and zooms[200] == 2.0          # full zoom from 3.5 s, held to 8.0 s
    assert zooms[213] == 1.0                               # back to normal after 8.5 s
    assert zooms[81] == pytest.approx(1.5, abs=0.1)        # middle of the ramp
    ramp_in = zooms[75:89]
    assert ramp_in == sorted(ramp_in)
    assert ramp_in[1] - ramp_in[0] < ramp_in[6] - ramp_in[5]  # eased: slow start


def test_crop_is_centred_on_the_point():
    zoom, x, y = apply_zoom.frame_at(5.0, [(3, 700, 300, 8)], config([]), SIZE)
    assert zoom == 2.0
    # the crop is 640x360, its centre is the mouse point
    assert (x + 640 / 2, y + 360 / 2) == (700, 300)


@pytest.mark.parametrize("point, expected", [
    ((10, 10), (0, 0)),             # top-left corner
    ((1275, 715), (640, 360)),      # bottom-right corner: iw - iw/zoom
    ((1275, 10), (640, 0)),
])
def test_crop_stays_inside_the_frame(point, expected):
    _, x, y = apply_zoom.frame_at(5.0, [(3, *point, 8)], config([]), SIZE)
    assert (x, y) == expected


def test_crop_formula_from_the_task():
    # x = clamp(px - iw/(2*zoom), 0, iw - iw/zoom)
    assert apply_zoom.crop_origin(640, 1280, 2) == 320
    assert apply_zoom.crop_origin(100, 1280, 2) == 0
    assert apply_zoom.crop_origin(1200, 1280, 2) == 640
    assert apply_zoom.crop_origin(500, 1280, 1) == 0  # no zoom: the whole frame


def test_mouse_to_video_mapping():
    assert apply_zoom.map_point(640, 360, VIEWPORT, (1280, 720)) == (640, 360)  # 1:1
    assert apply_zoom.map_point(640, 360, VIEWPORT, (1920, 1080)) == (960, 540)  # scaled


# ---------- ffmpeg filter and command (no ffmpeg needed) ----------

def test_filter_has_every_pair_and_keeps_the_size():
    cfg = config([ev(1, "in", 100, 200), ev(2, "out"), ev(4, "in", 900, 500)])
    pairs, _ = apply_zoom.pair_events(cfg["events"], cfg["ramp_sec"])
    chain = apply_zoom.build_filter(pairs, cfg, SIZE)
    assert chain.startswith("fps=25,zoompan=")
    assert ":d=1:s=1280x720:fps=25" in chain
    assert "between(it,1,2.5)" in chain and "between(it,4,1000000000.5)" in chain  # open-ended
    assert ",100," in chain and ",900," in chain and ",200," in chain and ",500," in chain
    assert "iw-iw/zoom" in chain and "ih-ih/zoom" in chain


def test_command_is_vp9_webm():
    cmd = apply_zoom.ffmpeg_command("ffmpeg", "in.webm", "out.webm", "fps=25")
    assert cmd[:2] == ["ffmpeg", "-hide_banner"]
    assert cmd[cmd.index("-c:v") + 1] == "libvpx-vp9"
    assert cmd[-1] == "out.webm"


def write_sidecar(video, events, **kwargs):
    data = {"viewport": VIEWPORT, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5, "events": events}
    data.update(kwargs)
    with open(apply_zoom.sidecar_path(video), "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_dry_run_prints_the_command_and_encodes_nothing(tmp_path, capsys):
    video = tmp_path / "ACME_GmbH.webm"
    video.write_bytes(b"\x1a\x45\xdf\xa3 not a real video")
    write_sidecar(str(video), [ev(3, "in"), ev(8, "out")])
    # A path that does not exist: dry-run must not need ffmpeg at all.
    code = apply_zoom.main(["--video", str(video), "--dry-run", "--ffmpeg", str(tmp_path / "no-ffmpeg")])
    out = capsys.readouterr().out
    assert code == 0
    assert "zoompan=" in out and "libvpx-vp9" in out and "ACME_GmbH.zoomed.webm" in out
    assert sorted(os.listdir(tmp_path)) == ["ACME_GmbH.webm", "ACME_GmbH.zoom.json"]


@pytest.mark.parametrize("events", [None, []])
def test_no_events_copies_unchanged(tmp_path, capsys, events):
    video = tmp_path / "v.webm"
    video.write_bytes(b"\x1a\x45\xdf\xa3" + os.urandom(100))
    if events is not None:
        write_sidecar(str(video), events)
    assert apply_zoom.main(["--video", str(video), "--ffmpeg", str(tmp_path / "no-ffmpeg")]) == 0
    assert (tmp_path / "v.zoomed.webm").read_bytes() == video.read_bytes()
    assert "copied unchanged" in capsys.readouterr().out


def test_missing_ffmpeg_is_a_clear_error(tmp_path, capsys):
    video = tmp_path / "v.webm"
    video.write_bytes(b"x")
    write_sidecar(str(video), [ev(1, "in")])
    assert apply_zoom.main(["--video", str(video), "--ffmpeg", str(tmp_path / "no-ffmpeg")]) == 2
    assert "ffmpeg not found" in capsys.readouterr().err


def test_bad_sidecar_is_exit_2(tmp_path, capsys):
    video = tmp_path / "v.webm"
    video.write_bytes(b"x")
    write_sidecar(str(video), [{"t": "soon", "x": 1, "y": 1}])
    assert apply_zoom.main(["--video", str(video), "--dry-run"]) == 2
    assert "events[0].t" in capsys.readouterr().err


# ---------- real ffmpeg render ----------

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
SQUARE = (900, 200)  # centre of a 20x20 red square in the synthetic video


def make_video(path, seconds=6):
    """Grey 1280x720 VP8 webm at 25 fps (like Playwright's) with a red square."""
    x, y = SQUARE[0] - 10, SQUARE[1] - 10
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=gray:s=1280x720:r=25:d={seconds}",
        "-vf", f"drawbox=x={x}:y={y}:w=20:h=20:color=red:t=fill",
        "-c:v", "libvpx", "-b:v", "1M", str(path),
    ], check=True)


def frame(path, t):
    """RGB bytes of the frame at time t."""
    proc = subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-i", str(path), "-ss", f"{t}",
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ], check=True, capture_output=True)
    assert len(proc.stdout) == 1280 * 720 * 3
    return proc.stdout


def red_box(rgb):
    """(centre_x, centre_y, width) of the red pixels."""
    xs, ys = [], []
    for i in range(0, len(rgb), 3):
        r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
        if r > 180 and g < 90 and b < 90:
            n = i // 3
            xs.append(n % 1280)
            ys.append(n // 1280)
    assert xs, "no red square in the frame"
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, max(xs) - min(xs) + 1


@needs_ffmpeg
def test_render_zooms_on_the_exact_point(tmp_path):
    video = tmp_path / "v.webm"
    make_video(video)
    write_sidecar(str(video), [ev(1.0, "in", *SQUARE), ev(4.0, "out", *SQUARE)])
    assert apply_zoom.main(["--video", str(video)]) == 0
    out = tmp_path / "v.zoomed.webm"
    assert apply_zoom.probe_size(FFMPEG, str(out)) == (1280, 720)  # same resolution

    x, y, w = red_box(frame(out, 0.5))         # before the zoom: unchanged
    assert (abs(x - 900) <= 2, abs(y - 200) <= 2, abs(w - 20) <= 2) == (True, True, True)
    x, y, w = red_box(frame(out, 2.5))         # hold: the point is in the middle, 2x bigger
    assert abs(x - 640) <= 3 and abs(y - 360) <= 3 and abs(w - 40) <= 3
    x, y, w = red_box(frame(out, 5.0))         # after the zoom-out: back to normal
    assert abs(x - 900) <= 2 and abs(y - 200) <= 2 and abs(w - 20) <= 2


@needs_ffmpeg
def test_render_open_ended_zoom_clamped_to_the_frame(tmp_path):
    video = tmp_path / "v.webm"
    make_video(video, seconds=4)
    # Zoom on a point near the top-right corner with no "out": the crop sticks to the
    # frame edges (x = iw - iw/zoom = 640, y = 0) and holds to the last frame.
    write_sidecar(str(video), [ev(1.0, "in", 1270, 5)])
    assert apply_zoom.main(["--video", str(video)]) == 0
    out = tmp_path / "v.zoomed.webm"
    for t in (2.0, 3.9):
        x, y, w = red_box(frame(out, t))
        # square at (900, 200) -> ((900 - 640) * 2, (200 - 0) * 2)
        assert abs(x - 520) <= 3 and abs(y - 400) <= 3 and abs(w - 40) <= 3


def test_sync_is_parsed_and_calibration_moves_events():
    cfg = config([ev(2.5, "in"), ev(5.5, "out")], sync={"t": 3.0, "size": 12})
    assert cfg["sync"] == {"t": 3.0, "size": 12}
    moved = apply_zoom.calibrate(cfg, 1.5, log=lambda m: None)  # marker gone at 1.5 s in the video
    assert [e["t"] for e in moved["events"]] == [1.0, 4.0]
    assert [e["t"] for e in cfg["events"]] == [2.5, 5.5]  # the input is not changed


@needs_ffmpeg
def test_render_uses_the_sync_marker(tmp_path):
    """The video has the magenta sync square for its first 1.5 s; the recorder says it
    removed it at 3.0 s on its clock. So every event happened 1.5 s earlier in the video."""
    video = tmp_path / "v.webm"
    x, y = SQUARE[0] - 10, SQUARE[1] - 10
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=gray:s=1280x720:r=25:d=6",
        "-vf", f"drawbox=x={x}:y={y}:w=20:h=20:color=red:t=fill,"
               "drawbox=x=0:y=0:w=12:h=12:color=0xff00ff:t=fill:enable='lt(t,1.5)'",
        "-c:v", "libvpx", "-b:v", "1M", str(video),
    ], check=True)
    assert apply_zoom.find_sync(FFMPEG, str(video), 12) == pytest.approx(1.5, abs=0.05)
    write_sidecar(str(video), [ev(2.5, "in", *SQUARE), ev(5.5, "out", *SQUARE)], sync={"t": 3.0, "size": 12})
    assert apply_zoom.main(["--video", str(video)]) == 0
    out = tmp_path / "v.zoomed.webm"
    x, y, w = red_box(frame(out, 2.5))   # 1.0 + 0.5 s ramp: fully zoomed, without the shift it would not be
    assert abs(x - 640) <= 3 and abs(y - 360) <= 3 and abs(w - 40) <= 3
    x, y, w = red_box(frame(out, 5.0))   # out at 4.0 + 0.5 s ramp: back to normal
    assert abs(x - 900) <= 2 and abs(w - 20) <= 2


@needs_ffmpeg
def test_no_marker_in_the_video_keeps_the_times(tmp_path, capsys):
    video = tmp_path / "v.webm"
    make_video(video, seconds=3)
    assert apply_zoom.find_sync(FFMPEG, str(video), 12) is None
    write_sidecar(str(video), [ev(1.0, "in", *SQUARE)], sync={"t": 3.0, "size": 12})
    assert apply_zoom.main(["--video", str(video), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "sync marker not found" in out and "in at 1 s" in out
