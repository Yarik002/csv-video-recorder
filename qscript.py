"""Browser automation that records a video of one scenario.

CaptureVideo_Magento(...) opens the page from the CSV row, shows an SVG cursor
with a yellow highlight, glides to the search box, types a query like a person,
submits it and clicks the first result. Playwright records the whole session
into a .webm file named after the company.
"""

import json
import math
import os
import random
import re
import shutil
import time
from typing import Tuple

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

# Videos go here. The folder is created if missing.
# capture_video.py may override this value at startup.
PATH_TO_VIDEO_FOLDER = "./video"

# The browser runs with a window by default. In headless mode DuckDuckGo
# answers the search with its error page (static-pages/home-error/418.html),
# so the scenario cannot reach the results. Headless is fine for other sites
# and for the offline tests.
HEADLESS = False

VIDEO_SIZE = {"width": 1280, "height": 720}

# DuckDuckGo selectors from the task.
SEARCH_BOX = 'textarea[name="q"]'
SEARCH_BUTTON = 'form button[type="submit"]'
FIRST_RESULT = 'a[data-testid="result-title-a"]'

QUERY_TEMPLATE = "Magento developer in {city}"

# Internship task 2: zoom events. F5 (or window.__zoomAt(x, y) from the scenario)
# toggles a zoom at the mouse point; apply_zoom.py renders it after recording.
# ZOOM_IN_SCENARIO = True (capture_video.py --zoom): the sample scenario zooms in on
# the search box while it types the query.
ZOOM_IN_SCENARIO = False
ZOOM_FACTOR = 2.0
ZOOM_RAMP_SEC = 0.5
VIDEO_FPS = 25  # Playwright writes 25 fps
SYNC_AFTER_SEC = 3.0  # the sync marker disappears this long after the context is created
SYNC_SIZE = 12        # px, magenta square in the top-left corner

PAGE_TIMEOUT_MS = 30_000
# Results are rendered client-side and can take 10+ seconds.
RESULTS_TIMEOUT_MS = 30_000

# Everything visual lives in the page. Plain IIFE, because add_init_script
# evaluates the string as is. Only element.style and element.animate are used:
# they are not blocked by a page's CSP, unlike <style> tags. SVG is built with
# createElementNS, because innerHTML is blocked on sites with Trusted Types.
OVERLAY_JS = r"""
(() => {
  if (window.__qsOverlay) return;
  window.__qsOverlay = true;

  const Z = 2147483647;
  const CURSOR = 32;          // arrow size, px
  const HIGHLIGHT_R = 32;     // yellow circle radius = cursor width
  const RIPPLE_R = 40;        // ripple ends a little past the highlight
  const KEY = '__qs_cursor';
  const SVG_NS = 'http://www.w3.org/2000/svg';

  let x = -100, y = -100;
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY));
    if (saved) { x = saved.x; y = saved.y; }
  } catch (e) {}

  function box(styles) {
    const el = document.createElement('div');
    Object.assign(el.style, {
      position: 'fixed', left: '0px', top: '0px', margin: '0', padding: '0',
      pointerEvents: 'none', zIndex: String(Z),
    }, styles);
    return el;
  }

  const highlight = box({
    width: HIGHLIGHT_R * 2 + 'px', height: HIGHLIGHT_R * 2 + 'px',
    borderRadius: '50%', background: 'rgba(255, 221, 0, 0.45)',
    zIndex: String(Z - 2),
  });
  highlight.setAttribute('data-qs', 'highlight');

  const cursor = box({ width: CURSOR + 'px', height: CURSOR + 'px' });
  cursor.setAttribute('data-qs', 'cursor');
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('width', CURSOR);
  svg.setAttribute('height', CURSOR);
  svg.setAttribute('viewBox', '0 0 32 32');
  const arrow = document.createElementNS(SVG_NS, 'path');
  // The tip is at (2, 2), so the box is shifted by -2px to put it on the pointer.
  arrow.setAttribute('d', 'M2 2 L2 27 L8.5 20.5 L13 30 L17.5 28 L13 18.5 L22 18.5 Z');
  arrow.setAttribute('fill', '#ffffff');
  arrow.setAttribute('stroke', '#000000');
  arrow.setAttribute('stroke-width', '2');
  arrow.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(arrow);
  cursor.appendChild(svg);

  function mount() {
    const root = document.documentElement;
    if (!root) return false;
    // Some pages rebuild the DOM, so put the elements back if they were removed.
    if (!highlight.isConnected) root.appendChild(highlight);
    if (!cursor.isConnected) root.appendChild(cursor);
    return true;
  }

  function place() {
    highlight.style.transform = `translate(${x - HIGHLIGHT_R}px, ${y - HIGHLIGHT_R}px)`;
    cursor.style.transform = `translate(${x - 2}px, ${y - 2}px)`;
  }

  // Ring that grows from the click point a bit past the yellow circle and fades
  // out. A strong orange edge so it stays visible over the highlight.
  let scriptedRippleAt = 0;
  function ripple(cx, cy) {
    if (!mount()) return;
    const el = box({
      width: RIPPLE_R * 2 + 'px', height: RIPPLE_R * 2 + 'px',
      borderRadius: '50%', border: '4px solid rgba(255, 87, 0, 0.95)',
      background: 'rgba(255, 140, 0, 0.18)', boxSizing: 'border-box',
      transform: `translate(${cx - RIPPLE_R}px, ${cy - RIPPLE_R}px) scale(0.15)`,
      zIndex: String(Z - 1),
    });
    el.setAttribute('data-qs', 'ripple');
    document.documentElement.appendChild(el);
    const at = `translate(${cx - RIPPLE_R}px, ${cy - RIPPLE_R}px)`;
    el.animate(
      [
        { transform: `${at} scale(0.15)`, opacity: 1 },
        { transform: `${at} scale(0.8)`, opacity: 0.9, offset: 0.6 },
        { transform: `${at} scale(1)`, opacity: 0 },
      ],
      { duration: 650, easing: 'ease-out', fill: 'forwards' }
    ).onfinish = () => el.remove();
  }

  // Called by Python right before a scripted click: a click that starts a
  // navigation would otherwise remove the ripple before it is ever painted.
  window.__qsRipple = (cx, cy) => { scriptedRippleAt = Date.now(); ripple(cx, cy); };

  window.addEventListener('mousemove', (e) => {
    x = e.clientX; y = e.clientY;
    if (mount()) place();
    try { sessionStorage.setItem(KEY, JSON.stringify({ x, y })); } catch (err) {}
  }, true);

  // Any other click (or a page without the Python helper) still gets a ripple.
  window.addEventListener('mousedown', (e) => {
    if (Date.now() - scriptedRippleAt > 800) ripple(e.clientX, e.clientY);
  }, true);

  // Yellow box with text at the top of the page. fade = true: 2 s fade-in and fade-out.
  // Resolves when the callout is gone.
  window.ShowCallout = function (text, bgColor, textColor, fade, holdMs) {
    if (!mount()) return Promise.resolve();
    const fadeMs = fade === false ? 0 : 2000;
    const hold = holdMs == null ? 1500 : holdMs;
    const el = box({
      top: '80px', left: '50%', transform: 'translateX(-50%)',
      maxWidth: '80vw', width: 'max-content',
      background: bgColor || '#ffeb3b', color: textColor || '#000000',
      font: '16pt/1.35 Arial, Helvetica, sans-serif', textAlign: 'center',
      padding: '8px 22px', borderRadius: '2px',
      boxShadow: '0 2px 8px rgba(0, 0, 0, 0.3)', whiteSpace: 'pre-wrap',
      zIndex: String(Z - 3),
    });
    el.setAttribute('data-qs', 'callout');
    el.textContent = text;
    document.documentElement.appendChild(el);
    const total = fadeMs * 2 + hold;
    if (total === 0) { el.remove(); return Promise.resolve(); }
    const keyframes = fadeMs
      ? [
          { opacity: 0, offset: 0 },
          { opacity: 1, offset: fadeMs / total },
          { opacity: 1, offset: (fadeMs + hold) / total },
          { opacity: 0, offset: 1 },
        ]
      : [{ opacity: 1 }, { opacity: 1 }];
    const anim = el.animate(keyframes, { duration: total, fill: 'forwards' });
    return anim.finished.then(() => el.remove(), () => el.remove());
  };

  // Init scripts run before <html> exists. Mount as soon as it appears, not at
  // DOMContentLoaded: slow pages (DuckDuckGo results) stay blank for seconds,
  // and the cursor should stay on screen during that time.
  if (mount()) {
    place();
  } else {
    const observer = new MutationObserver(() => {
      if (mount()) { place(); observer.disconnect(); }
    });
    observer.observe(document, { childList: true });
  }
})();
"""


# Zoom hotkey and hook. The state (zoomed or not) and the event list live in
# Python (ZoomRecorder), because the page changes on every navigation. The time
# is taken in the page (Date.now()) at the moment of the key press: Python may be
# busy in time.sleep() and handle the call a bit later.
#
# Sync marker: Playwright's video starts 1-2 s after the context is created (the
# delay changes from run to run), so "time since the context" is not "time in the
# video". A small magenta square in the top-left corner is shown from the start
# and removed at a known moment (SYNC_AFTER_SEC); apply_zoom.py finds the frame
# where it disappears and shifts the events by the difference.
ZOOM_JS = r"""
(() => {
  if (window.__qsZoom) return;
  window.__qsZoom = true;

  const Z = 2147483647;
  const SYNC_AT = __SYNC_AT__;   // Date.now() value when the marker disappears
  const SYNC_SIZE = __SYNC_SIZE__;

  if (Date.now() < SYNC_AT) {
    const sync = document.createElement('div');
    Object.assign(sync.style, {
      position: 'fixed', left: '0px', top: '0px', margin: '0', padding: '0',
      width: SYNC_SIZE + 'px', height: SYNC_SIZE + 'px', background: '#ff00ff',
      pointerEvents: 'none', zIndex: String(Z),
    });
    sync.setAttribute('data-qs', 'sync');
    const put = () => {
      if (!document.documentElement) return false;
      document.documentElement.appendChild(sync);
      return true;
    };
    if (!put()) {
      const obs = new MutationObserver(() => { if (put()) obs.disconnect(); });
      obs.observe(document, { childList: true });
    }
    setTimeout(() => {
      sync.remove();
      if (typeof window.__qsZoomSync === 'function') window.__qsZoomSync(Date.now());
    }, SYNC_AT - Date.now());
  }
  let mx = -1, my = -1;     // last mouse position in this page
  try {
    const saved = JSON.parse(sessionStorage.getItem('__qs_cursor'));
    if (saved) { mx = saved.x; my = saved.y; }
  } catch (e) {}
  window.addEventListener('mousemove', (e) => { mx = e.clientX; my = e.clientY; }, true);

  // Our guess of the state, for the ring colour; Python has the real one.
  let zoomed = false;
  if (typeof window.__qsZoomState === 'function') {
    window.__qsZoomState().then((v) => { zoomed = !!v; }, () => {});
  }

  // Indicator: a ring at the point, green = zoom in starts, red = zoom out starts.
  function ring(x, y, type) {
    const root = document.documentElement;
    if (!root) return;
    const R = 46;
    const el = document.createElement('div');
    Object.assign(el.style, {
      position: 'fixed', left: '0px', top: '0px', margin: '0', padding: '0',
      width: R * 2 + 'px', height: R * 2 + 'px', borderRadius: '50%',
      border: '5px solid ' + (type === 'in' ? 'rgba(0, 200, 83, 0.95)' : 'rgba(229, 57, 53, 0.95)'),
      boxSizing: 'border-box', pointerEvents: 'none', zIndex: String(Z - 1),
      transform: `translate(${x - R}px, ${y - R}px)`,
    });
    el.setAttribute('data-qs', 'zoom-' + type);
    root.appendChild(el);
    el.animate([{ opacity: 1 }, { opacity: 1, offset: 0.6 }, { opacity: 0 }],
               { duration: 900, fill: 'forwards' }).onfinish = () => el.remove();
  }

  function toggle(x, y) {
    x = Math.round(x); y = Math.round(y);
    zoomed = !zoomed;
    ring(x, y, zoomed ? 'in' : 'out');
    if (typeof window.__qsZoomEvent !== 'function') return Promise.resolve(null);
    return window.__qsZoomEvent(Date.now(), x, y).then((type) => {
      zoomed = type === 'in';
      return type;
    });
  }

  // For the automation scenario: same toggle as F5. Resolves to "in" or "out".
  window.__zoomAt = (x, y) => toggle(x, y);

  window.addEventListener('keydown', (e) => {
    if (e.key !== 'F5') return;
    e.preventDefault();       // no page reload
    e.stopPropagation();
    if (e.repeat) return;     // holding the key down is one press
    const x = mx >= 0 ? mx : innerWidth / 2, y = my >= 0 ? my : innerHeight / 2;
    toggle(x, y);
  }, true);
})();
"""


class ZoomRecorder:
    """Collects zoom events during one recording. t = seconds since start_time,
    the moment the browser context (and with it the video) was created."""

    def __init__(self, start_time: float, viewport=None) -> None:
        self.start_time = start_time  # time.time()
        self.viewport = dict(viewport or VIDEO_SIZE)
        self.zoomed = False
        self.events = []
        self.sync_time = None  # time.time() when the sync marker was removed

    def attach(self, context) -> None:
        context.expose_binding("__qsZoomState", lambda source: self.zoomed)
        context.expose_binding("__qsZoomEvent", lambda source, ms, x, y: self.toggle(ms / 1000, x, y))
        context.expose_binding("__qsZoomSync", lambda source, ms: self._synced(ms / 1000))
        sync_at_ms = int((self.start_time + SYNC_AFTER_SEC) * 1000)
        context.add_init_script(
            ZOOM_JS.replace("__SYNC_AT__", str(sync_at_ms)).replace("__SYNC_SIZE__", str(SYNC_SIZE)))

    def _synced(self, when: float) -> None:
        if self.sync_time is None:  # the first page that removed the marker
            self.sync_time = when

    def toggle(self, when: float, x: float, y: float) -> str:
        """when: time.time() of the key press. Returns the event type."""
        self.zoomed = not self.zoomed
        kind = "in" if self.zoomed else "out"
        t = round(max(0.0, when - self.start_time), 3)
        self.events.append({"t": t, "x": int(round(x)), "y": int(round(y)), "type": kind})
        return kind

    def sidecar(self) -> dict:
        data = {
            "viewport": self.viewport,
            "fps": VIDEO_FPS,
            "zoom_factor": ZOOM_FACTOR,
            "ramp_sec": ZOOM_RAMP_SEC,
            "events": sorted(self.events, key=lambda e: e["t"]),
        }
        if self.sync_time is not None:
            # Same clock as the events: apply_zoom.py moves both so that this
            # moment lands on the frame where the marker disappears.
            data["sync"] = {"t": round(self.sync_time - self.start_time, 3), "size": SYNC_SIZE}
        return data

    def write_sidecar(self, video_path: str):
        """Writes <video>.zoom.json only when there is at least one event; otherwise
        removes an old sidecar, so a new video never gets the zoom of an old one.
        Returns the path or None."""
        path = os.path.splitext(video_path)[0] + ".zoom.json"
        if not self.events:
            if os.path.exists(path):
                os.remove(path)
            return None
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.sidecar(), f, indent=2)
        return path


def zoom_at(page: Page, x: float, y: float) -> str:
    """Toggles the zoom from the scenario (the automation does not press F5)."""
    return page.evaluate("([x, y]) => window.__zoomAt(x, y)", [x, y])


def _wait_until_still(loc, timeout_ms: int, still_ms: int = 600) -> None:
    """Waits until the element stops moving: results and suggestions keep
    shifting while they load, and a cursor aimed at a moving target misses."""
    deadline = time.time() + timeout_ms / 1000
    last, still_since = None, time.time()
    while time.time() < deadline:
        bb = loc.bounding_box()
        box = None if bb is None else tuple(round(v) for v in bb.values())
        if box != last:
            last, still_since = box, time.time()
        elif box is not None and time.time() - still_since >= still_ms / 1000:
            return
        time.sleep(0.1)
    # Still moving after the timeout: go on, glide_to_element re-aims anyway.


class _Mouse:
    """Moves the real Playwright mouse along a smooth curve and remembers where it is."""

    def __init__(self, page: Page, x: float, y: float) -> None:
        self.page = page
        self.x = x
        self.y = y

    def show(self) -> None:
        # After a navigation the new page knows nothing about the mouse.
        # A tiny move fires mousemove, and the overlay draws the cursor again.
        self.page.mouse.move(self.x + 1, self.y)
        self.page.mouse.move(self.x, self.y)

    def glide_to(self, x: float, y: float) -> None:
        dist = math.hypot(x - self.x, y - self.y)
        if dist < 1:
            return
        duration = min(1.4, 0.35 + dist / 900)
        steps = max(12, int(duration * 60))
        # A slight arc looks more like a hand than a straight line.
        bend = random.uniform(-0.12, 0.12) * dist
        nx, ny = -(y - self.y) / dist, (x - self.x) / dist
        cx = (self.x + x) / 2 + nx * bend
        cy = (self.y + y) / 2 + ny * bend
        sx, sy = self.x, self.y
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)  # ease in-out
            px = (1 - e) ** 2 * sx + 2 * (1 - e) * e * cx + e * e * x
            py = (1 - e) ** 2 * sy + 2 * (1 - e) * e * cy + e * e * y
            self.page.mouse.move(px, py)
            time.sleep(duration / steps)
        self.x, self.y = x, y

    def glide_to_element(self, selector: str, timeout: int) -> Tuple[float, float]:
        # First *visible* match: on DuckDuckGo the first submit button in the
        # form is a hidden 0 px wide one, the visible one appears after typing.
        loc = self.page.locator(f"{selector} >> visible=true").first
        loc.wait_for(state="visible", timeout=timeout)
        loc.scroll_into_view_if_needed(timeout=timeout)
        _wait_until_still(loc, min(timeout, 5000))
        # The target can move while the cursor travels (DuckDuckGo's suggestion
        # list grows as suggestions arrive), so check again after each glide.
        for _ in range(4):
            bb = loc.bounding_box()
            if bb is None:
                raise RuntimeError(f"element has no box: {selector}")
            if (bb["x"] <= self.x <= bb["x"] + bb["width"]
                    and bb["y"] <= self.y <= bb["y"] + bb["height"]):
                return self.x, self.y
            # Aim near the centre, not at the exact pixel.
            tx = bb["x"] + bb["width"] * random.uniform(0.35, 0.6)
            ty = bb["y"] + bb["height"] * random.uniform(0.4, 0.6)
            self.glide_to(tx, ty)
            time.sleep(0.15)
        raise RuntimeError(f"element keeps moving: {selector}")

    def click(self) -> None:
        # Ripple first and let it play for a moment: if the click navigates,
        # the old page is gone right after mouseup.
        try:
            self.page.evaluate("([x, y]) => window.__qsRipple && window.__qsRipple(x, y)",
                               [self.x, self.y])
            time.sleep(0.3)
        except PlaywrightError:
            pass  # decoration only
        # Then real input events (down, short pause, up), so default actions work.
        self.page.mouse.down()
        time.sleep(0.12)
        self.page.mouse.up()


def ShowCallout(page: Page, text: str, bgColor: str = "#ffeb3b", textColor: str = "#000000",
                fade: bool = True, hold_ms: int = 1500) -> None:
    """Shows a callout and waits until it disappears (fade-in 2 s, hold, fade-out 2 s).

    The callout is decoration: if the page navigates meanwhile and the JS
    context is destroyed, the scenario goes on.
    """
    try:
        page.evaluate(
            "([t, bg, fg, fade, hold]) => window.ShowCallout(t, bg, fg, fade, hold)",
            [text, bgColor, textColor, fade, hold_ms],
        )
    except PlaywrightError as exc:
        if "context was destroyed" not in str(exc) and "navigation" not in str(exc).lower():
            raise


def _type_like_human(page: Page, text: str) -> None:
    for ch in text:
        page.keyboard.type(ch)
        delay = random.uniform(0.06, 0.16)
        if ch == " ":
            delay += random.uniform(0.05, 0.2)
        time.sleep(delay)


def _locale(language: str) -> str:
    """CSV Language -> browser locale: "de" -> "de", "pt_br" -> "pt-BR".
    Anything that is not a language code falls back to English, so the page
    language does not depend on the machine that records the video."""
    m = re.fullmatch(r"([A-Za-z]{2,3})(?:[-_]([A-Za-z]{2}))?", language.strip())
    if not m:
        return "en-US"
    return m.group(1).lower() + ("-" + m.group(2).upper() if m.group(2) else "")


def _safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "video"


def _run_scenario(page: Page, url: str, city: str, company_name: str, steps: list) -> None:
    """Runs the sample behaviour. Appends the name of each step to `steps`,
    so a failure can say where it happened."""
    steps.append("open the page")
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_selector(f"{SEARCH_BOX} >> visible=true", timeout=PAGE_TIMEOUT_MS)

    steps.append("show the cursor and the callout")
    mouse = _Mouse(page, VIDEO_SIZE["width"] * 0.75, VIDEO_SIZE["height"] * 0.8)
    mouse.show()
    ShowCallout(page, f"{company_name}: searching for a Magento developer in {city}", hold_ms=800)

    steps.append("click the search box")
    mouse.glide_to_element(SEARCH_BOX, PAGE_TIMEOUT_MS)
    mouse.click()
    time.sleep(0.3)
    if ZOOM_IN_SCENARIO:
        steps.append("zoom in on the search box")
        zoom_at(page, mouse.x, mouse.y)
        time.sleep(ZOOM_RAMP_SEC)
    steps.append("type the query")
    _type_like_human(page, QUERY_TEMPLATE.format(city=city))
    time.sleep(0.5)
    if ZOOM_IN_SCENARIO:
        steps.append("zoom out")
        zoom_at(page, mouse.x, mouse.y)
        time.sleep(ZOOM_RAMP_SEC)

    steps.append("click the search button")
    mouse.glide_to_element(SEARCH_BUTTON, PAGE_TIMEOUT_MS)
    # The search is a full page navigation and the old JS context is destroyed.
    # So nothing runs in the page here: Python waits for the results selector,
    # and wait_for_selector keeps working across the navigation.
    mouse.click()
    steps.append("wait for the results")
    page.wait_for_selector(f"{FIRST_RESULT} >> visible=true", timeout=RESULTS_TIMEOUT_MS)
    mouse.show()
    time.sleep(1.0)

    steps.append("click the first result")
    results_url = page.url
    mouse.glide_to_element(FIRST_RESULT, PAGE_TIMEOUT_MS)
    time.sleep(0.4)
    mouse.click()
    page.wait_for_url(lambda u: u != results_url, wait_until="commit", timeout=PAGE_TIMEOUT_MS)
    try:
        page.wait_for_load_state("load", timeout=15_000)
    except PlaywrightError:
        pass  # a slow third-party site should not fail the recording
    mouse.show()
    time.sleep(2.5)


def CaptureVideo_Magento(
    url,
    company_name,
    language,
    country,
    postcode,
    city,
    street,
    house_no,
):
    """Records the sample scenario for one CSV row.

    All arguments are strings from the CSV. The sample scenario uses url, city,
    company_name and language (browser locale); the address fields are
    accepted for the real Magento flow.
    Returns the absolute path to <PATH_TO_VIDEO_FOLDER>/<company_name>.webm,
    or None on failure.
    """
    folder = os.path.abspath(PATH_TO_VIDEO_FOLDER)
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, _safe_filename(company_name) + ".webm")

    ok = False
    raw_video = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS)
            try:
                context = browser.new_context(
                    locale=_locale(language),
                    viewport=VIDEO_SIZE,
                    record_video_dir=folder,
                    record_video_size=VIDEO_SIZE,  # == viewport: mouse px = video px
                )
                # t = 0 of the zoom events: the moment the context (and the video) starts.
                zoom = ZoomRecorder(time.time(), VIDEO_SIZE)
                context.add_init_script(OVERLAY_JS)
                zoom.attach(context)
                page = context.new_page()
                steps = []
                try:
                    _run_scenario(page, url, city, company_name, steps)
                    ok = True
                except Exception as exc:
                    first_line = str(exc).splitlines()[0] if str(exc) else ""
                    print(f"  failed at step '{steps[-1] if steps else '?'}': "
                          f"{exc.__class__.__name__}: {first_line}")
                finally:
                    # The video is written only when the context is closed.
                    raw_video = page.video.path() if page.video else None
                    context.close()
            finally:
                browser.close()
    except Exception as exc:
        print(f"  browser error for {company_name!r}: {exc.__class__.__name__}: {exc}")
        return None

    if raw_video is None or not os.path.exists(raw_video):
        return None
    if not ok:
        # Keep the recording for debugging, but do not report it as a result.
        failed = os.path.join(folder, _safe_filename(company_name) + ".failed.webm")
        shutil.move(raw_video, failed)
        print(f"  partial video kept: {failed}")
        return None
    if os.path.exists(target):
        os.remove(target)
    shutil.move(raw_video, target)
    sidecar = zoom.write_sidecar(target)
    if sidecar:
        print(f"  zoom events: {len(zoom.events)} -> {sidecar}")
    return target
