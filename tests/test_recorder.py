"""Offline tests: a local copy of the search flow instead of DuckDuckGo.

Run: python -m unittest -v
"""

import contextlib
import functools
import http.server
import io
import os
import shutil
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

import capture_video
import qscript

SITE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "site")


class _Handler(http.server.SimpleHTTPRequestHandler):
    requests = []

    def do_GET(self):
        _Handler.requests.append(self.path)
        super().do_GET()

    def log_message(self, *args):
        pass


class LocalSite:
    def __enter__(self):
        _Handler.requests = []
        handler = functools.partial(_Handler, directory=SITE)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def requests(self):
        return list(_Handler.requests)


def write_csv(folder, text):
    path = os.path.join(folder, "input.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    return path


class ReadRowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_quoted_semicolon_bom_and_empty_lines(self):
        path = write_csv(
            self.tmp,
            "﻿URL;Company Name;Language;Country;Postcode;City;Street;HouseNo\n"
            'https://a.test/;"Home; Garden AB";sv;Sweden;111 52;Göteborg;"Main ""Old"" St";5\n'
            "\n"
            " https://b.test/ ;B Ltd;en;UK;M1;Leeds;High St;1\n",
        )
        rows = capture_video.read_rows(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Company Name"], "Home; Garden AB")
        self.assertEqual(rows[0]["City"], "Göteborg")
        self.assertEqual(rows[0]["Street"], 'Main "Old" St')
        self.assertEqual(rows[1]["URL"], "https://b.test/")

    def test_missing_column(self):
        path = write_csv(self.tmp, "URL;Company Name;City\nhttps://a.test/;A;Riga\n")
        with self.assertRaises(ValueError) as ctx:
            capture_video.read_rows(path)
        self.assertIn("HouseNo", str(ctx.exception))

    def test_locale(self):
        self.assertEqual(qscript._locale("de"), "de")
        self.assertEqual(qscript._locale(" pt_br "), "pt-BR")
        self.assertEqual(qscript._locale("German"), "en-US")
        self.assertEqual(qscript._locale(""), "en-US")

    def test_safe_filename(self):
        self.assertEqual(qscript._safe_filename('A/B: "C"?'), "A_B_ _C__")
        self.assertEqual(qscript._safe_filename("  ..  "), "video")


class OverlayTest(unittest.TestCase):
    """Checks the injected cursor, highlight, ripple and callout in a real browser."""

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.site = LocalSite().__enter__()
        self.context = self.browser.new_context(viewport=qscript.VIDEO_SIZE)
        self.context.add_init_script(qscript.OVERLAY_JS)
        self.page = self.context.new_page()
        self.page.goto(self.site.base + "index.html")

    def tearDown(self):
        self.context.close()
        self.site.__exit__(None, None, None)

    def rect(self, kind):
        return self.page.evaluate(
            """(k) => { const el = document.querySelector(`[data-qs="${k}"]`);
            if (!el) return null;
            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
            return {x: r.x, y: r.y, w: r.width, h: r.height, pe: s.pointerEvents,
                    z: s.zIndex, bg: s.backgroundColor, font: s.fontSize}; }""",
            kind,
        )

    def test_cursor_and_highlight_follow_mouse(self):
        self.page.mouse.move(300, 200)
        cur, hl = self.rect("cursor"), self.rect("highlight")
        self.assertEqual((cur["w"], cur["h"]), (32, 32))
        self.assertEqual((cur["x"] + 2, cur["y"] + 2), (300, 200))  # arrow tip on the pointer
        self.assertEqual(cur["pe"], "none")
        self.assertEqual(cur["z"], "2147483647")
        # Highlight: circle with radius = cursor width, centred on the pointer, behind the arrow.
        self.assertEqual((hl["x"] + hl["w"] / 2, hl["y"] + hl["h"] / 2), (300, 200))
        self.assertEqual(hl["w"], 64)
        self.assertLess(int(hl["z"]), int(cur["z"]))
        self.assertTrue(hl["bg"].startswith("rgba(255, 221, 0, 0.4"), hl["bg"])
        self.assertEqual(hl["pe"], "none")
        arrow = self.page.evaluate(
            "() => { const p = document.querySelector('[data-qs=cursor] path');"
            " return [p.getAttribute('fill'), p.getAttribute('stroke')]; }"
        )
        self.assertEqual(arrow, ["#ffffff", "#000000"])

    def test_overlay_does_not_intercept_clicks(self):
        box = self.page.locator('textarea[name="q"]').bounding_box()
        x, y = box["x"] + 20, box["y"] + box["height"] / 2
        self.page.mouse.move(x, y)
        hit = self.page.evaluate(f"document.elementFromPoint({x}, {y}).tagName")
        self.assertEqual(hit, "TEXTAREA")
        self.page.mouse.click(x, y)
        self.page.keyboard.type("abc")
        self.assertEqual(self.page.input_value('textarea[name="q"]'), "abc")

    def test_click_shows_ripple(self):
        self.page.mouse.move(400, 300)
        self.page.mouse.down()
        ripple = self.rect("ripple")
        self.page.mouse.up()
        self.assertIsNotNone(ripple)
        self.page.wait_for_timeout(900)
        self.assertIsNone(self.rect("ripple"), "ripple should fade out and be removed")

    def test_callout(self):
        # No return: evaluate would wait for the promise, i.e. until the callout is gone.
        self.page.evaluate("() => { window.ShowCallout('Hello', '#ffeb3b', '#000000', false, 2000); }")
        callout = self.rect("callout")
        self.assertEqual(callout["bg"], "rgb(255, 235, 59)")
        self.assertEqual(callout["font"], "21.3333px")  # 16pt
        self.assertEqual(callout["pe"], "none")
        self.assertEqual(self.page.inner_text("[data-qs=callout]"), "Hello")

    def test_cursor_survives_navigation(self):
        self.page.mouse.move(250, 150)
        self.page.goto(self.site.base + "target.html")
        cur = self.rect("cursor")
        self.assertEqual((cur["x"] + 2, cur["y"] + 2), (250, 150))


class CaptureVideoTest(unittest.TestCase):
    """End to end: CSV -> capture_video.main -> CaptureVideo_Magento -> .webm files."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (qscript.PATH_TO_VIDEO_FOLDER, qscript.PAGE_TIMEOUT_MS, qscript.HEADLESS)

    def tearDown(self):
        qscript.PATH_TO_VIDEO_FOLDER, qscript.PAGE_TIMEOUT_MS, qscript.HEADLESS = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_csv_loop_records_videos(self):
        video_dir = os.path.join(self.tmp, "nested", "video")  # does not exist yet
        qscript.PAGE_TIMEOUT_MS = 8000
        with LocalSite() as site:
            csv_path = write_csv(
                self.tmp,
                "URL;Company Name;Language;Country;Postcode;City;Street;HouseNo\n"
                f"{site.base}index.html;Good Co;en;UK;M1;Testville;High St;1\n"
                f"{site.base}empty.html;Broken Co;en;UK;M1;Nowhere;High St;2\n",
            )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = capture_video.main([csv_path, "--video-dir", video_dir, "--headless"])
            requests = site.requests

        log = out.getvalue()
        self.assertEqual(code, 1, log)  # one row failed
        expected = os.path.join(os.path.abspath(video_dir), "Good Co.webm")
        self.assertIn(f"saved: {expected}", log)
        self.assertGreater(os.path.getsize(expected), 10_000)
        with open(expected, "rb") as f:
            self.assertEqual(f.read(4), b"\x1a\x45\xdf\xa3")  # WebM/Matroska header

        # The query was typed and submitted, then the first result was opened.
        searches = [parse_qs(urlparse(r).query) for r in requests if r.startswith("/results.html")]
        self.assertEqual(searches[0]["q"], ["Magento developer in Testville"])
        self.assertIn("/target.html?n=1", requests)

        # The failed row returns None and leaves only a debug video.
        self.assertIn("FAILED", log)
        self.assertEqual(
            sorted(os.listdir(video_dir)), ["Broken Co.failed.webm", "Good Co.webm"]
        )


if __name__ == "__main__":
    unittest.main()
