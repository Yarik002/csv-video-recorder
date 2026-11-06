"""Task 2, client side: video_client.py and capture_video.py --server.

A real web_service runs in a thread on a free port, with its data in a temp folder.
The preview opens a Chromium window (headless=False, as the task says), so on a
machine without a screen run the tests under xvfb-run (see .github/workflows/ci.yml).
"""

import builtins
import contextlib
import inspect
import io
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import requests
from werkzeug.serving import make_server  # tests only; the service itself does not import werkzeug

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # for test_recorder

import capture_video  # noqa: E402
import qscript  # noqa: E402
import video_client  # noqa: E402
import web_service  # noqa: E402
from test_recorder import LocalSite, write_csv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE = os.path.join(ROOT, "docs", "sample.webm")  # a real recording from Task 1
logging.getLogger("werkzeug").setLevel(logging.ERROR)  # no request log lines in the test output
CSV_HEADER = "URL;Company Name;Language;Country;Postcode;City;Street;HouseNo\n"


class LiveServer:
    """web_service on 127.0.0.1:<free port>, records the paths it was asked for."""

    def __enter__(self):
        self.tmp = tempfile.mkdtemp()
        self.app = web_service.create_app(self.tmp)
        self.paths = []
        self.app.after_request(self._log)
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def _log(self, response):
        from flask import request

        self.paths.append(request.path)
        return response

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rows(self):
        conn = sqlite3.connect(self.app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM videos ORDER BY id")]
        finally:
            conn.close()


def free_port():
    """A port nobody listens on, for the "server is down" cases."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_main(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = capture_video.main(argv)
    return code, out.getvalue()


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.server = LiveServer().__enter__()

    def tearDown(self):
        self.server.__exit__(None, None, None)

    def test_exact_signatures(self):
        self.assertEqual(
            str(inspect.signature(video_client.UploadVideo)),
            "(server_url: str, video_path: str, company_name: str) -> str",
        )
        self.assertEqual(
            str(inspect.signature(video_client.GetVideoURL)),
            "(server_url: str, company_name: str) -> str",
        )

    def test_upload_then_get_url(self):
        url = video_client.UploadVideo(self.server.url + "/", SAMPLE, "ACME GmbH")
        self.assertEqual(url, self.server.url + "/media/ACME_GmbH.webm")
        self.assertEqual(video_client.GetVideoURL(self.server.url, "ACME GmbH"), url)

        resp = requests.get(url, timeout=10)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["Content-Type"].startswith("video/webm"))
        with open(SAMPLE, "rb") as f:
            self.assertEqual(resp.content, f.read())

    def test_unknown_company(self):
        with self.assertRaises(video_client.VideoNotFoundError) as ctx:
            video_client.GetVideoURL(self.server.url, "Nobody Ltd")
        self.assertIsInstance(ctx.exception, LookupError)

    def test_server_error_text_is_shown(self):
        bad = os.path.join(self.server.tmp, "notes.txt")
        with open(bad, "w") as f:
            f.write("not a video")
        with self.assertRaises(video_client.VideoServiceError) as ctx:
            video_client.UploadVideo(self.server.url, bad, "ACME GmbH")
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("unsupported file type", str(ctx.exception))

    def test_server_down(self):
        down = f"http://127.0.0.1:{free_port()}"
        for call in (
            lambda: video_client.UploadVideo(down, SAMPLE, "ACME GmbH"),
            lambda: video_client.GetVideoURL(down, "ACME GmbH"),
        ):
            with self.assertRaises(video_client.VideoServiceError) as ctx:
                call()
            self.assertIn("cannot reach", str(ctx.exception))

    def test_preview_opens_the_url(self):
        url = video_client.UploadVideo(self.server.url, SAMPLE, "ACME GmbH")
        video_client.PreviewVideo(url, 0.5)
        self.assertIn("/media/ACME_GmbH.webm", self.server.paths)

    def test_preview_zero_waits_for_enter(self):
        url = video_client.UploadVideo(self.server.url, SAMPLE, "ACME GmbH")
        with mock.patch.object(builtins, "input", return_value="") as fake_input:
            video_client.PreviewVideo(url, 0)
        fake_input.assert_called_once()
        self.assertIn("/media/ACME_GmbH.webm", self.server.paths)


class LazyImportTest(unittest.TestCase):
    def test_recorder_does_not_import_requests(self):
        code = (
            "import sys, capture_video; "
            "print(sorted(m for m in ('requests', 'video_client', 'flask') if m in sys.modules))"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(out.strip(), "[]")

    def test_runs_without_requests_unless_server_is_given(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        csv_path = write_csv(tmp, CSV_HEADER + "https://a.test/;A Co;en;UK;M1;Leeds;High St;1\n")
        with mock.patch.dict(sys.modules, {"requests": None}), \
                mock.patch.object(qscript, "CaptureVideo_Magento", return_value=SAMPLE):
            code, log = run_main([csv_path])
            self.assertEqual(code, 0, log)
            self.assertIn("Done: 1 of 1 videos saved.", log)

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _ = run_main([csv_path, "--server", "http://127.0.0.1:1"])
            self.assertEqual(code, 2)
            self.assertIn("needs the requests package", err.getvalue())


class BatchLoopTest(unittest.TestCase):
    """capture_video --server: a failed upload or preview never stops the loop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.csv = write_csv(
            self.tmp,
            CSV_HEADER
            + "https://a.test/;A Co;en;UK;M1;Leeds;High St;1\n"
            + "https://b.test/;B Co;en;UK;M1;York;High St;2\n",
        )
        # The recording itself is tested in test_recorder.py; here it returns a ready file.
        patcher = mock.patch.object(qscript, "CaptureVideo_Magento", return_value=SAMPLE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_server_down_loop_goes_on(self):
        code, log = run_main([self.csv, "--server", f"http://127.0.0.1:{free_port()}"])
        self.assertEqual(code, 1, log)
        self.assertEqual(log.count("upload failed: cannot reach"), 2, log)
        self.assertIn("[2/2] B Co", log)
        self.assertIn("Done: 2 of 2 videos saved, 0 uploaded, 0 previewed.", log)

    def test_preview_failure_loop_goes_on(self):
        with LiveServer() as server, \
                mock.patch.object(video_client, "PreviewVideo", side_effect=RuntimeError("no display")):
            code, log = run_main([self.csv, "--server", server.url, "--view-seconds", "1"])
            names = [r["company_name"] for r in server.rows()]
        self.assertEqual(code, 1, log)
        self.assertEqual(log.count("preview failed: RuntimeError: no display"), 2, log)
        self.assertEqual(names, ["A Co", "B Co"])
        self.assertIn("Done: 2 of 2 videos saved, 2 uploaded, 0 previewed.", log)

    def test_view_seconds_is_passed_and_checked(self):
        with LiveServer() as server, mock.patch.object(video_client, "PreviewVideo") as preview:
            code, log = run_main([self.csv, "--server", server.url, "--view-seconds", "0"])
        self.assertEqual(code, 0, log)
        preview.assert_called_with(server.url + "/media/B_Co.webm", 0.0)

        with LiveServer() as server, mock.patch.object(video_client, "PreviewVideo") as preview:
            run_main([self.csv, "--server", server.url])
        preview.assert_called_with(server.url + "/media/B_Co.webm", 10.0)  # default

        for argv in (["--view-seconds", "-1", "--server", "x"], ["--view-seconds", "5"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                capture_video.main([self.csv] + argv)


class EndToEndTest(unittest.TestCase):
    """Real recording on the local search copy -> real upload -> real preview window."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.saved = (qscript.PATH_TO_VIDEO_FOLDER, qscript.PAGE_TIMEOUT_MS, qscript.HEADLESS)

    def tearDown(self):
        qscript.PATH_TO_VIDEO_FOLDER, qscript.PAGE_TIMEOUT_MS, qscript.HEADLESS = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_record_upload_preview(self):
        qscript.PAGE_TIMEOUT_MS = 8000
        video_dir = os.path.join(self.tmp, "video")
        with LocalSite() as site, LiveServer() as server:
            csv_path = write_csv(
                self.tmp,
                CSV_HEADER
                + f"{site.base}index.html;Good Co;en;UK;M1;Testville;High St;1\n"
                + f"{site.base}empty.html;Broken Co;en;UK;M1;Nowhere;High St;2\n",
            )
            code, log = run_main([csv_path, "--video-dir", video_dir, "--headless",
                                  "--server", server.url, "--view-seconds", "1"])
            rows = server.rows()
            paths = list(server.paths)
            stored = os.path.join(server.app.config["UPLOAD_FOLDER"], "Good_Co.webm")
            with open(stored, "rb") as f:
                stored_bytes = f.read()

        self.assertEqual(code, 1, log)  # the broken row
        url = server.url + "/media/Good_Co.webm"
        self.assertIn(f"  uploaded: {url}", log)
        self.assertIn("Done: 1 of 2 videos saved, 1 uploaded, 1 previewed.", log)
        self.assertEqual([(r["company_name"], r["video_url"]) for r in rows], [("Good Co", url)])
        self.assertEqual(rows[0]["file_path"], stored)
        with open(os.path.join(video_dir, "Good Co.webm"), "rb") as f:
            self.assertEqual(stored_bytes, f.read())
        self.assertEqual(paths[0], "/upload")  # uploaded, then opened in the browser
        self.assertIn("/media/Good_Co.webm", paths[1:])


if __name__ == "__main__":
    unittest.main()
