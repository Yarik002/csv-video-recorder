"""Internship task 2: zoom capture in qscript.py, in a real (headless) Chromium.

F5 and window.__zoomAt(x, y) toggle the zoom, the events go to <video>.zoom.json
only when there is at least one. Offline: the local copy of the search site.

Run: python -m pytest tests/test_zoom_capture.py -v
"""

import contextlib
import io
import json
import os
import sys
import time

import pytest
from playwright.sync_api import sync_playwright

import apply_zoom
import capture_video
import qscript

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_recorder import LocalSite, write_csv  # noqa: E402


@pytest.fixture
def browser():
    # Per test: a second sync_playwright() (capture_video.main) cannot run while one is open.
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def site():
    with LocalSite() as s:
        yield s


@pytest.fixture
def recorded(browser, site):
    """A page with the zoom script, like in CaptureVideo_Magento. Returns (page, recorder)."""
    context = browser.new_context(viewport=qscript.VIDEO_SIZE)
    recorder = qscript.ZoomRecorder(time.time())
    context.add_init_script(qscript.OVERLAY_JS)
    recorder.attach(context)
    page = context.new_page()
    page.goto(site.base + "index.html")
    yield page, recorder
    context.close()


def test_zoom_at_toggles_in_and_out(recorded):
    page, rec = recorded
    assert qscript.zoom_at(page, 400, 250) == "in"
    assert page.locator('[data-qs="zoom-in"]').count() == 1  # the green ring
    time.sleep(0.3)
    assert qscript.zoom_at(page, 410.6, 260.2) == "out"
    assert page.locator('[data-qs="zoom-out"]').count() == 1  # the red ring
    assert [(e["type"], e["x"], e["y"]) for e in rec.events] == [("in", 400, 250), ("out", 411, 260)]
    t_in, t_out = rec.events[0]["t"], rec.events[1]["t"]
    assert 0 < t_in < t_out
    assert t_out - t_in == pytest.approx(0.3, abs=0.15)


def test_f5_toggles_at_the_mouse_and_does_not_reload(recorded):
    page, rec = recorded
    page.evaluate("window.__notReloaded = true")
    page.mouse.move(300, 200)
    page.keyboard.press("F5")
    page.mouse.move(700, 500)
    page.keyboard.press("F5")
    page.wait_for_function("() => document.querySelector('[data-qs=\"zoom-out\"]')")
    page.evaluate("() => new Promise(r => setTimeout(r, 100))")  # let the binding calls arrive
    assert page.evaluate("window.__notReloaded") is True
    assert [(e["type"], e["x"], e["y"]) for e in rec.events] == [("in", 300, 200), ("out", 700, 500)]


def test_state_survives_a_navigation(recorded, site):
    page, rec = recorded
    assert qscript.zoom_at(page, 100, 100) == "in"
    page.goto(site.base + "results.html?q=x")
    page.mouse.move(50, 60)
    page.keyboard.press("F5")
    page.wait_for_function("() => document.querySelector('[data-qs=\"zoom-out\"]')")  # red: the page knew
    page.evaluate("() => new Promise(r => setTimeout(r, 100))")
    assert [e["type"] for e in rec.events] == ["in", "out"]


def test_sidecar_only_with_events(tmp_path):
    rec = qscript.ZoomRecorder(time.time())
    video = str(tmp_path / "ACME_GmbH.webm")
    assert rec.write_sidecar(video) is None
    assert os.listdir(tmp_path) == []

    rec.toggle(rec.start_time + 3.0, 640, 360)
    rec.toggle(rec.start_time + 8.0, 640, 360)
    path = rec.write_sidecar(video)
    assert path == str(tmp_path / "ACME_GmbH.zoom.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data == {  # the format from the task
        "viewport": {"width": 1280, "height": 720}, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5,
        "events": [{"t": 3.0, "x": 640, "y": 360, "type": "in"}, {"t": 8.0, "x": 640, "y": 360, "type": "out"}],
    }
    apply_zoom.parse_sidecar(data)  # apply_zoom.py reads it

    # A new recording of the same company without zoom: the old sidecar must go.
    assert qscript.ZoomRecorder(time.time()).write_sidecar(video) is None
    assert os.listdir(tmp_path) == []


def test_sync_marker(browser, site, monkeypatch):
    monkeypatch.setattr(qscript, "SYNC_AFTER_SEC", 1.0)
    context = browser.new_context(viewport=qscript.VIDEO_SIZE)
    rec = qscript.ZoomRecorder(time.time())
    rec.attach(context)
    try:
        page = context.new_page()
        page.goto(site.base + "index.html")
        box = page.locator('[data-qs="sync"]').bounding_box()
        assert box == {"x": 0, "y": 0, "width": qscript.SYNC_SIZE, "height": qscript.SYNC_SIZE}
        page.wait_for_selector('[data-qs="sync"]', state="detached", timeout=5000)
        page.evaluate("() => new Promise(r => setTimeout(r, 100))")
        assert rec.sync_time - rec.start_time == pytest.approx(1.0, abs=0.1)
        rec.toggle(time.time(), 1, 1)
        assert rec.sidecar()["sync"]["t"] == pytest.approx(1.0, abs=0.1)
    finally:
        context.close()


def test_capture_video_zoom_writes_the_sidecar(tmp_path, site, monkeypatch):
    """capture_video.py --zoom: the scenario zooms in on the search box while it types."""
    monkeypatch.setattr(qscript, "PATH_TO_VIDEO_FOLDER", qscript.PATH_TO_VIDEO_FOLDER)
    monkeypatch.setattr(qscript, "HEADLESS", qscript.HEADLESS)
    monkeypatch.setattr(qscript, "ZOOM_IN_SCENARIO", False)
    monkeypatch.setattr(qscript, "PAGE_TIMEOUT_MS", 8000)
    video_dir = tmp_path / "video"
    csv_path = write_csv(str(tmp_path), "URL;Company Name;Language;Country;Postcode;City;Street;HouseNo\n"
                                        f"{site.base}index.html;Zoom Co;en;UK;M1;Testville;High St;1\n")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = capture_video.main([csv_path, "--video-dir", str(video_dir), "--headless", "--zoom"])
    assert code == 0, out.getvalue()
    assert sorted(os.listdir(video_dir)) == ["Zoom Co.webm", "Zoom Co.zoom.json"]
    assert "zoom events: 2" in out.getvalue()
    data = apply_zoom.load_sidecar(str(video_dir / "Zoom Co.zoom.json"))
    pairs, _ = apply_zoom.pair_events(data["events"], data["ramp_sec"])
    (t_in, x, y, t_out), = pairs
    assert t_out - t_in > 2  # held while the query was typed
    # the point is inside the search box of the local page
    assert data["events"][0]["type"] == "in" and data["events"][1]["type"] == "out"
    assert 0 < x < 1280 and 0 < y < 720
    assert data["sync"]["t"] == pytest.approx(qscript.SYNC_AFTER_SEC, abs=0.2)
