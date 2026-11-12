"""Camtasia-style zoom for a recorded video (Internship task 2).

Reads the zoom events that qscript.py wrote next to the video (<video>.zoom.json)
and renders a new .webm with ffmpeg's zoompan filter: a smooth zoom-in to the
mouse point, hold, a smooth zoom-out, for every in/out pair.

    python apply_zoom.py --video video/ACME_GmbH.webm             # -> video/ACME_GmbH.zoomed.webm
    python apply_zoom.py --video video/ACME_GmbH.webm --dry-run   # only print the ffmpeg command
    python apply_zoom.py --video ... --ffmpeg C:\\ffmpeg\\bin\\ffmpeg.exe

No sidecar or no events: the video is copied unchanged.
The math (pairing, easing, crop) is plain Python, the tests check it without ffmpeg.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

DEFAULTS = {"viewport": {"width": 1280, "height": 720}, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5}
FOREVER = 1e9  # end time of a zoom that has no "out": holds to the end of the video


class SidecarError(ValueError):
    """The sidecar file is not valid."""


# ---------- sidecar ----------

def sidecar_path(video_path):
    """video/ACME_GmbH.webm -> video/ACME_GmbH.zoom.json"""
    return os.path.splitext(video_path)[0] + ".zoom.json"


def _number(value, name, minimum=None, above=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise SidecarError(f"{name} must be a number, got {value!r}")
    if minimum is not None and value < minimum:
        raise SidecarError(f"{name} must be >= {minimum}, got {value!r}")
    if above is not None and value <= above:
        raise SidecarError(f"{name} must be > {above}, got {value!r}")
    return value


def parse_sidecar(data):
    """Checks the sidecar dict and fills in defaults. Returns a new dict.

    Events are sorted by time. An event without "type" (old sidecars) is a
    toggle: it means "in" when the zoom is off and "out" when it is on."""
    if not isinstance(data, dict):
        raise SidecarError("the sidecar must be a JSON object")
    viewport = data.get("viewport", DEFAULTS["viewport"])
    if not isinstance(viewport, dict):
        raise SidecarError("viewport must be an object with width and height")
    result = {
        "viewport": {
            "width": int(_number(viewport.get("width"), "viewport.width", above=0)),
            "height": int(_number(viewport.get("height"), "viewport.height", above=0)),
        },
        "fps": _number(data.get("fps", DEFAULTS["fps"]), "fps", above=0),
        "zoom_factor": _number(data.get("zoom_factor", DEFAULTS["zoom_factor"]), "zoom_factor", minimum=1),
        "ramp_sec": _number(data.get("ramp_sec", DEFAULTS["ramp_sec"]), "ramp_sec", minimum=0),
    }
    events = data.get("events", [])
    if not isinstance(events, list):
        raise SidecarError("events must be a list")
    parsed = []
    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise SidecarError(f"events[{i}] must be an object")
        kind = ev.get("type")
        if kind not in (None, "in", "out"):
            raise SidecarError(f"events[{i}].type must be 'in' or 'out', got {kind!r}")
        parsed.append({
            "t": float(_number(ev.get("t"), f"events[{i}].t", minimum=0)),
            "x": float(_number(ev.get("x"), f"events[{i}].x")),
            "y": float(_number(ev.get("y"), f"events[{i}].y")),
            "type": kind,
        })
    parsed.sort(key=lambda ev: ev["t"])  # stable: equal times keep their order
    zoomed = False
    for ev in parsed:
        if ev["type"] is None:
            ev["type"] = "out" if zoomed else "in"
        zoomed = ev["type"] == "in"
    result["events"] = parsed
    sync = data.get("sync")
    if sync is not None:
        if not isinstance(sync, dict):
            raise SidecarError("sync must be an object with t and size")
        result["sync"] = {
            "t": float(_number(sync.get("t"), "sync.t", minimum=0)),
            "size": int(_number(sync.get("size", 12), "sync.size", minimum=4)),
        }
    return result


def load_sidecar(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as exc:
        raise SidecarError(f"{path}: not valid JSON: {exc}") from exc
    return parse_sidecar(data)


# ---------- zoom math ----------

def pair_events(events, ramp_sec):
    """[{t, x, y, type}] -> [(t_in, x, y, t_out)], t_out is None for an open zoom.

    Each "in" is paired with the next "out". A second "in" while zoomed and an
    "out" while not zoomed are ignored. Pairs must not overlap: a zoom that
    starts before the previous zoom-out ramp has finished is moved to start
    right after it. Returns (pairs, warnings)."""
    pairs, warnings = [], []
    start = None
    for ev in events:
        if ev["type"] == "in":
            if start is not None:
                warnings.append(f"t={ev['t']:g}: 'in' while already zoomed, ignored")
                continue
            start = ev
        else:
            if start is None:
                warnings.append(f"t={ev['t']:g}: 'out' without 'in', ignored")
                continue
            pairs.append([start["t"], start["x"], start["y"], ev["t"]])
            start = None
    if start is not None:
        pairs.append([start["t"], start["x"], start["y"], None])  # holds to the end

    result = []
    for t_in, x, y, t_out in pairs:
        if result:
            prev_out = result[-1][3]
            free_at = prev_out + ramp_sec  # previous zoom-out ramp ends here
            if t_in < free_at:
                warnings.append(f"t={t_in:g}: zoom starts before the previous one ended, moved to {free_at:g}")
                t_in = free_at
                if t_out is not None and t_out <= t_in:
                    warnings.append(f"t={t_in:g}: nothing left of this zoom, dropped")
                    continue
        result.append((t_in, x, y, t_out))
    return result, warnings


def ease(p):
    """Smoothstep: starts and ends slowly (ease-in / ease-out), 0 -> 0, 1 -> 1."""
    p = min(max(p, 0.0), 1.0)
    return p * p * (3 - 2 * p)


def effective_ramp(config):
    """ramp_sec, but at least one frame (ramp 0 = jump in one frame, no division by zero)."""
    return max(config["ramp_sec"], 1.0 / config["fps"])


def zoom_level(t, pairs, ramp):
    """0 = no zoom, 1 = full zoom, at time t (seconds). Same formula as the ffmpeg expression."""
    for t_in, _, _, t_out in pairs:
        end = FOREVER if t_out is None else t_out
        if t_in <= t <= end + ramp:
            return ease(min((t - t_in) / ramp, (end + ramp - t) / ramp))
    return 0.0


def crop_origin(point, size, zoom):
    """Left (or top) edge of the crop window: centred on the point, kept inside the frame.
    clamp(px - iw/(2*zoom), 0, iw - iw/zoom)"""
    return min(max(point - size / (2 * zoom), 0.0), size - size / zoom)


def map_point(x, y, viewport, video_size):
    """Mouse position in the page (CSS px) -> pixel in the video. 1:1 when
    record_video_size == viewport, scaled otherwise."""
    return x * video_size[0] / viewport["width"], y * video_size[1] / viewport["height"]


def frame_at(t, pairs, config, video_size):
    """Python reference for one frame: (zoom, crop_x, crop_y)."""
    ramp = effective_ramp(config)
    level = zoom_level(t, pairs, ramp)
    zoom = 1 + (config["zoom_factor"] - 1) * level
    px = py = 0.0
    for t_in, x, y, t_out in pairs:
        end = FOREVER if t_out is None else t_out
        if t_in <= t <= end + ramp:
            px, py = map_point(x, y, config["viewport"], video_size)
            break
    return zoom, crop_origin(px, video_size[0], zoom), crop_origin(py, video_size[1], zoom)


# ---------- ffmpeg ----------

def _f(value):
    return f"{value:.4f}".rstrip("0").rstrip(".")


def build_filter(pairs, config, video_size):
    """The -vf filter chain. zoompan expressions use "it" (time of the input frame)
    and the registers st()/ld() to keep the zoom level of the active pair."""
    width, height = video_size
    fps = config["fps"]
    ramp = effective_ramp(config)
    level_parts, px_expr, py_expr = [], "0", "0"
    for t_in, x, y, t_out in reversed(pairs):
        end = FOREVER if t_out is None else t_out
        px, py = map_point(x, y, config["viewport"], video_size)
        inside = f"between(it,{_f(t_in)},{_f(end + ramp)})"
        # p = progress of the ramp: min(time since in, time to the end of out) / ramp
        level_parts.append(
            f"st(0,clip(min(it-{_f(t_in)},{_f(end + ramp)}-it)/{_f(ramp)},0,1));"
            f"st(1,ld(1)+{inside}*ld(0)*ld(0)*(3-2*ld(0)))"
        )
        px_expr = f"if({inside},{_f(px)},{px_expr})"
        py_expr = f"if({inside},{_f(py)},{py_expr})"
    z_expr = "st(1,0);" + ";".join(level_parts) + f";1+{_f(config['zoom_factor'] - 1)}*ld(1)"
    # crop window iw/zoom x ih/zoom centred on the point, clamped to the frame
    x_expr = f"clip({px_expr}-iw/zoom/2,0,iw-iw/zoom)"
    y_expr = f"clip({py_expr}-ih/zoom/2,0,ih-ih/zoom)"
    return (
        f"fps={_f(fps)},"
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}':d=1:s={width}x{height}:fps={_f(fps)},"
        "setsar=1,format=yuv420p"
    )


def ffmpeg_command(ffmpeg, video, output, filter_chain):
    return [
        ffmpeg, "-hide_banner", "-y", "-i", video,
        "-vf", filter_chain,
        "-an",  # Playwright videos have no sound
        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32",
        "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
        "-f", "webm",  # the output may be a temporary name like x.webm.part
        output,
    ]


def find_ffmpeg(path=None):
    """--ffmpeg path, or ffmpeg from PATH. None if not found."""
    if path:
        return path if os.path.isfile(path) else shutil.which(path)
    return shutil.which("ffmpeg")


def probe_size(ffmpeg, video):
    """(width, height) of the first video stream, read from "ffmpeg -i" output."""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", video], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    m = re.search(r"Stream #.*?Video: .*?(\d{2,5})x(\d{2,5})", proc.stderr)
    if not m:
        raise RuntimeError(f"cannot read the video size of {video}: {proc.stderr.strip()[-300:]}")
    return int(m.group(1)), int(m.group(2))


def find_sync(ffmpeg, video, size, search_sec=20):
    """Time (s) of the first frame where the magenta sync square in the top-left
    corner has disappeared, or None if the video does not start with it.
    Reads the mean colour of the inside of the square in every frame (showinfo)."""
    inner = max(size - 4, 2)
    proc = subprocess.run([
        ffmpeg, "-hide_banner", "-t", str(search_sec), "-i", video,
        "-vf", f"crop={inner}:{inner}:2:2,showinfo", "-f", "null", "-",
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    seen = False
    for m in re.finditer(r"pts_time:\s*([\d.]+).*?mean:\[(\d+) (\d+) (\d+)\]", proc.stderr):
        t, u, v = float(m.group(1)), int(m.group(3)), int(m.group(4))
        magenta = u > 180 and v > 180  # YUV of #ff00ff is about (105, 212, 235)
        if magenta:
            seen = True
        elif seen:
            return t
    return None


def calibrate(config, video_sync_t, log=print):
    """Moves the events so that sync.t (recorder clock) lands on video_sync_t (video clock)."""
    shift = config["sync"]["t"] - video_sync_t
    log(f"sync marker: gone at {video_sync_t:g} s in the video, {config['sync']['t']:g} s on the "
        f"recorder clock -> events moved by {-shift:+.3f} s")
    config = dict(config)
    config["events"] = [dict(ev, t=max(0.0, round(ev["t"] - shift, 3))) for ev in config["events"]]
    return config


def show_command(cmd):
    return subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(_sh_quote(a) for a in cmd)


def _sh_quote(arg):
    if re.fullmatch(r"[\w@%+=:,./-]+", arg):
        return arg
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def default_output(video):
    return os.path.splitext(video)[0] + ".zoomed.webm"


def render(video, output, config, ffmpeg=None, dry_run=False, log=print):
    """Renders the zoomed video. config: a parsed sidecar (parse_sidecar).
    Returns the ffmpeg command (list), or None if the video was copied unchanged."""
    if not config["events"]:
        if dry_run:
            log(f"no zoom events: {video} would be copied unchanged to {output}")
        else:
            shutil.copyfile(video, output)
            log(f"no zoom events: copied unchanged to {output}")
        return None

    exe = find_ffmpeg(ffmpeg)
    if exe and config.get("sync"):
        found = find_sync(exe, video, config["sync"]["size"])
        if found is None:
            log("warning: sync marker not found in the video, event times used as they are")
        else:
            config = calibrate(config, found, log)
    pairs, warnings = pair_events(config["events"], config["ramp_sec"])
    for w in warnings:
        log(f"warning: {w}")
    if not pairs:  # only stray events
        return render(video, output, dict(config, events=[]), ffmpeg, dry_run, log)
    if exe:
        size = probe_size(exe, video)
    elif dry_run:
        exe = ffmpeg or "ffmpeg"
        vp = config["viewport"]
        size = (vp["width"], vp["height"])  # record_video_size == viewport
        log(f"ffmpeg not found, assuming the video is {size[0]}x{size[1]} (the viewport)"
            + (", sync marker not checked" if config.get("sync") else ""))
    else:
        raise FileNotFoundError("ffmpeg not found: install it or pass --ffmpeg <path>")

    for t_in, x, y, t_out in pairs:
        end = "end of video" if t_out is None else f"{t_out:g} s"
        log(f"zoom x{config['zoom_factor']:g} at ({x:g}, {y:g}): in at {t_in:g} s, out at {end}")
    cmd = ffmpeg_command(exe, video, output, build_filter(pairs, config, size))
    if dry_run:
        log(show_command(cmd))
        return cmd
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {proc.stderr.strip()[-800:]}")
    return cmd


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the zoom events of a recorded video with ffmpeg.")
    parser.add_argument("--video", required=True, help="the recorded .webm")
    parser.add_argument("--sidecar", help="zoom events (default: <video>.zoom.json next to the video)")
    parser.add_argument("--output", help="result (default: <video>.zoomed.webm)")
    parser.add_argument("--ffmpeg", help="path to ffmpeg if it is not on PATH")
    parser.add_argument("--dry-run", action="store_true", help="print the ffmpeg command, do not encode")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.video):
        print(f"no such video: {args.video}", file=sys.stderr)
        return 2
    output = args.output or default_output(args.video)
    sidecar = args.sidecar or sidecar_path(args.video)
    try:
        if os.path.isfile(sidecar):
            config = load_sidecar(sidecar)
        elif args.sidecar:
            print(f"no such sidecar: {sidecar}", file=sys.stderr)
            return 2
        else:
            print(f"no sidecar {sidecar}")
            config = parse_sidecar({})
        render(args.video, output, config, ffmpeg=args.ffmpeg, dry_run=args.dry_run)
    except (SidecarError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"saved: {os.path.abspath(output)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
