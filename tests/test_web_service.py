"""Tests for web_service.py: Flask test client plus one real threaded server.

Every test gets its own temporary folder with videos.db and uploads/.

Run: python -m unittest tests.test_web_service -v
"""

import contextlib
import io
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest

import requests
from werkzeug.serving import WSGIRequestHandler, make_server

import web_service


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args):
        pass  # no "POST /upload 200" line for every request


def fake_video(size=64):
    """WebM magic bytes plus random data, different on every call."""
    return b"\x1a\x45\xdf\xa3" + os.urandom(size)


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = web_service.create_app(self.tmp)
        self.client = self.app.test_client()
        self.uploads = os.path.join(os.path.abspath(self.tmp), "uploads")
        self.db_path = os.path.join(os.path.abspath(self.tmp), "videos.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def upload(self, company, data, filename="clip.webm", **kwargs):
        """POST /upload; None leaves the field out."""
        form = {}
        if company is not None:
            form["company_name"] = company
        if data is not None:
            form["file"] = (io.BytesIO(data), filename)
        return self.client.post("/upload", data=form, content_type="multipart/form-data", **kwargs)

    def sql(self, query, args=()):
        """Runs one statement on videos.db directly and returns all rows."""
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn:
            rows = conn.execute(query, args).fetchall()
            conn.commit()
        return rows

    def rows(self):
        return self.sql("SELECT company_name, file_path, video_url, id, created_at, updated_at FROM videos")

    def read(self, filename):
        with open(os.path.join(self.uploads, filename), "rb") as f:
            return f.read()

    def files(self):
        return sorted(os.listdir(self.uploads))

    def assertNothingStored(self):
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.files(), [])

    def assertJsonError(self, resp, status):
        self.assertEqual(resp.status_code, status)
        self.assertTrue(resp.is_json, resp.data)
        self.assertIn("error", resp.get_json())


class StartupTest(ServiceTestCase):
    def test_db_and_uploads_exist_before_any_request(self):
        self.assertTrue(os.path.isfile(self.db_path))
        self.assertTrue(os.path.isdir(self.uploads))

    def test_schema(self):
        columns = [(name, type_, notnull, pk) for _, name, type_, notnull, _, pk
                   in self.sql("PRAGMA table_info(videos)")]
        self.assertEqual(columns, [
            ("id", "INTEGER", 0, 1),
            ("company_name", "TEXT", 1, 0),
            ("file_path", "TEXT", 1, 0),
            ("video_url", "TEXT", 1, 0),
            ("created_at", "TEXT", 1, 0),
            ("updated_at", "TEXT", 1, 0),
        ])
        self.assertNotIn("BLOB", [c[1] for c in columns])
        table_sql = self.sql("SELECT sql FROM sqlite_master WHERE name = 'videos'")[0][0]
        self.assertIn("AUTOINCREMENT", table_sql)

    def test_company_name_is_unique(self):
        unique = [row for row in self.sql("PRAGMA index_list(videos)") if row[2] == 1]
        columns = [self.sql(f"PRAGMA index_info('{row[1]}')")[0][2] for row in unique]
        self.assertEqual(columns, ["company_name"])
        self.sql("INSERT INTO videos (company_name, file_path, video_url) VALUES ('X', 'a', 'b')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.sql("INSERT INTO videos (company_name, file_path, video_url) VALUES ('X', 'c', 'd')")

    def test_wal_mode(self):
        self.assertEqual(self.sql("PRAGMA journal_mode")[0][0], "wal")


class UploadTest(ServiceTestCase):
    def test_upload_returns_url_and_saves_file(self):
        data = fake_video()
        resp = self.upload("ACME GmbH", data)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "company_name": "ACME GmbH",
            "video_url": "http://localhost/media/ACME_GmbH.webm",
        })
        self.assertEqual(self.files(), ["ACME_GmbH.webm"])  # no .part leftovers
        self.assertEqual(self.read("ACME_GmbH.webm"), data)
        path = os.path.join(self.uploads, "ACME_GmbH.webm")
        self.assertEqual(self.rows()[0][:3], ("ACME GmbH", path, "http://localhost/media/ACME_GmbH.webm"))
        self.assertTrue(os.path.isabs(self.rows()[0][1]))

    def test_url_uses_request_host(self):
        resp = self.upload("ACME", fake_video(), base_url="http://10.1.2.3:8123")
        self.assertTrue(resp.get_json()["video_url"].startswith("http://10.1.2.3:8123/media/"))

    def test_reupload_updates_same_row(self):
        self.upload("ACME", fake_video())
        self.sql("UPDATE videos SET created_at = '2000-01-01 00:00:00', updated_at = '2000-01-01 00:00:00'")
        row_id = self.rows()[0][3]
        new_data = fake_video()
        self.assertEqual(self.upload("ACME", new_data).status_code, 200)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], row_id)
        self.assertEqual(rows[0][4], "2000-01-01 00:00:00")
        self.assertNotEqual(rows[0][5], "2000-01-01 00:00:00")
        self.assertEqual(self.read("ACME.webm"), new_data)

    def test_reupload_other_extension_removes_old_file(self):
        self.upload("ACME", fake_video())
        resp = self.upload("ACME", fake_video(), filename="clip.mp4")
        self.assertTrue(resp.get_json()["video_url"].endswith("/media/ACME.mp4"))
        self.assertEqual(self.files(), ["ACME.mp4"])
        self.assertEqual(len(self.rows()), 1)

    def test_similar_names_do_not_share_a_file(self):
        first, second = fake_video(), fake_video()
        self.upload("A B", first)
        url = self.upload("A_B", second).get_json()["video_url"]
        self.assertRegex(url, r"/media/A_B_[0-9a-f]{8}\.webm$")
        self.assertEqual(self.read("A_B.webm"), first)
        self.assertEqual(self.read(url.rsplit("/", 1)[1]), second)
        url_again = self.upload("A B", fake_video()).get_json()["video_url"]
        self.assertTrue(url_again.endswith("/media/A_B.webm"))
        self.assertEqual(len(self.files()), 2)

    def test_names_differing_only_in_case_get_different_files(self):
        # On Windows and macOS acme.webm and ACME.webm are one file.
        first, second = fake_video(), fake_video()
        self.upload("acme", first)
        url = self.upload("ACME", second).get_json()["video_url"]
        self.assertRegex(url, r"/media/ACME_[0-9a-f]{8}\.webm$")
        self.assertEqual(self.read("acme.webm"), first)
        self.assertEqual(len(self.files()), 2)

    def test_cyrillic_name(self):
        resp = self.upload("ООО Ромашка", fake_video())
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["company_name"], "ООО Ромашка")
        self.assertRegex(self.files()[0], r"^video_[0-9a-f]{8}\.webm$")
        lookup = self.client.get("/get_video_url", query_string={"company_name": "ООО Ромашка"})
        self.assertEqual(lookup.get_json(), body)

    def test_windows_reserved_name(self):
        self.assertEqual(self.upload("CON", fake_video()).status_code, 200)
        self.assertNotIn("CON.webm", self.files())
        self.assertRegex(self.files()[0], r"^video_[0-9a-f]{8}\.webm$")

    def test_spaces_around_name_are_stripped(self):
        self.upload(" ACME GmbH ", fake_video())
        resp = self.client.get("/get_video_url?company_name=ACME%20GmbH")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.rows()[0][0], "ACME GmbH")


class LookupAndListTest(ServiceTestCase):
    def test_get_video_url(self):
        self.upload("ACME GmbH", fake_video())
        resp = self.client.get("/get_video_url", query_string={"company_name": "ACME GmbH"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "company_name": "ACME GmbH",
            "video_url": "http://localhost/media/ACME_GmbH.webm",
        })

    def test_get_video_url_errors(self):
        self.assertJsonError(self.client.get("/get_video_url?company_name=Nobody"), 404)
        self.assertJsonError(self.client.get("/get_video_url"), 400)
        self.assertJsonError(self.client.get("/get_video_url?company_name=%20%20"), 400)

    def test_videos_lists_all_sorted(self):
        for name in ("Zeta", "Alpha", "Mid"):
            self.upload(name, fake_video())
        videos = self.client.get("/videos").get_json()["videos"]
        self.assertEqual([v["company_name"] for v in videos], ["Alpha", "Mid", "Zeta"])
        self.assertEqual(set(videos[0]), {"id", "company_name", "file_path", "video_url",
                                          "created_at", "updated_at"})


class ErrorTest(ServiceTestCase):
    def test_bad_uploads_store_nothing(self):
        cases = {
            "no file": self.upload("ACME", None),
            "no company": self.upload(None, fake_video()),
            "blank company": self.upload("   ", fake_video()),
            "empty file": self.upload("ACME", b""),
            "exe": self.upload("ACME", fake_video(), filename="virus.exe"),
        }
        for name, resp in cases.items():
            with self.subTest(name):
                self.assertJsonError(resp, 400)
        self.assertNothingStored()

    def test_get_upload_is_405_json(self):
        self.assertJsonError(self.client.get("/upload"), 405)

    def test_too_large_is_413_json(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 1000
        resp = self.upload("ACME", fake_video(2000))
        self.assertJsonError(resp, 413)
        self.assertIn("too large", resp.get_json()["error"])
        self.assertNothingStored()


class MediaTest(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.data = fake_video(100)
        self.upload("ACME", self.data)

    def get(self, url, **kwargs):
        resp = self.client.get(url, **kwargs)
        resp.close()  # Windows cannot delete a file that is still open
        return resp

    def test_serves_file(self):
        resp = self.client.get("/media/ACME.webm")
        body = resp.get_data()
        resp.close()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.headers["Content-Type"].startswith("video/webm"))
        self.assertEqual(resp.headers.get("Accept-Ranges"), "bytes")
        self.assertEqual(body, self.data)

    def test_range_request(self):
        resp = self.client.get("/media/ACME.webm", headers={"Range": "bytes=0-3"})
        body = resp.get_data()
        resp.close()
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(body, self.data[:4])
        self.assertEqual(resp.headers["Content-Range"], f"bytes 0-3/{len(self.data)}")

    def test_unknown_and_unsafe_paths_are_404(self):
        open(os.path.join(self.uploads, ".hidden.webm"), "wb").close()
        for url in ("/media/nope.webm", "/media/../videos.db", "/media/%2e%2e/videos.db",
                    "/media/x/../../videos.db", "/media/.hidden.webm"):
            with self.subTest(url):
                resp = self.get(url)
                self.assertEqual(resp.status_code, 404)
                self.assertNotIn(b"SQLite format", resp.get_data())


class ConcurrencyTest(ServiceTestCase):
    """Real threaded dev server, 20 uploads started at the same moment."""

    def run_parallel_uploads(self, companies):
        server = make_server("127.0.0.1", 0, self.app, threaded=True, request_handler=QuietHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/upload"
        jobs = [(name, fake_video(2000)) for name in companies for _ in range(2)]
        barrier = threading.Barrier(len(jobs))
        results = []

        def send(name, data):
            session = requests.Session()
            session.trust_env = False  # ignore proxy settings of this PC
            barrier.wait()
            try:
                resp = session.post(url, data={"company_name": name},
                                    files={"file": ("clip.webm", data)}, timeout=30)
                results.append((resp.status_code, resp.text))
            except requests.RequestException as exc:
                results.append((None, repr(exc)))
            finally:
                session.close()

        threads = [threading.Thread(target=send, args=job) for job in jobs]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
        finally:
            server.shutdown()
            server.server_close()

        self.assertEqual(len(results), len(jobs))
        for status, text in results:
            self.assertEqual(status, 200, text)
            self.assertNotIn("locked", text)
        payloads = {}
        for name, data in jobs:
            payloads.setdefault(name, []).append(data)
        return payloads

    def check_db_and_files(self, payloads):
        rows = self.rows()
        self.assertEqual(sorted(r[0] for r in rows), sorted(payloads))
        unique_paths = {os.path.normcase(r[1]).lower() for r in rows}
        self.assertEqual(len(unique_paths), len(rows), "two companies share a file")
        for name, path, url, *_ in rows:
            with open(path, "rb") as f:
                self.assertIn(f.read(), payloads[name], name)
            self.assertTrue(url.endswith("/" + os.path.basename(path)))
        self.assertEqual(len(self.files()), len(rows))  # no .part files, no orphans

    def test_parallel_uploads(self):
        payloads = self.run_parallel_uploads([f"Company {i}" for i in range(10)])
        self.check_db_and_files(payloads)

    def test_parallel_uploads_with_colliding_names(self):
        names = ["A B", "A_B", "A/B", "A+B", "a b", "a_B", "A@B", "A!B", "A__B", "_A_B"]
        payloads = self.run_parallel_uploads(names)
        self.check_db_and_files(payloads)


if __name__ == "__main__":
    unittest.main()
