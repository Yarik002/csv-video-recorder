"""Tests for web_service.py: Flask test client plus one real threaded server.

Every test gets an empty PostgreSQL test database (the table is dropped) and its
own temporary uploads/ folder. Database settings: see tests/db_helper.py.

Run: python -m unittest tests.test_web_service -v
"""

import contextlib
import datetime
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import psycopg2
import psycopg2.errors
import requests
from werkzeug.serving import WSGIRequestHandler, make_server

import web_service

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_helper import SETTINGS, needs_db, reset_db, sql  # noqa: E402


class QuietHandler(WSGIRequestHandler):
    def log_request(self, *args):
        pass  # no "POST /upload 200" line for every request


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fake_video(size=64):
    """WebM magic bytes plus random data, different on every call."""
    return b"\x1a\x45\xdf\xa3" + os.urandom(size)


@needs_db
class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        reset_db()
        self.app = web_service.create_app(self.tmp, db=SETTINGS)
        self.client = self.app.test_client()
        self.uploads = os.path.join(os.path.abspath(self.tmp), "uploads")

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
        """Runs one statement on the test database directly and returns all rows."""
        return sql(query, args)

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
    def test_table_and_uploads_exist_before_any_request(self):
        self.assertEqual(self.sql("SELECT to_regclass('videos') IS NOT NULL")[0][0], True)
        self.assertTrue(os.path.isdir(self.uploads))

    def test_schema(self):
        columns = self.sql(
            "SELECT column_name, data_type, is_nullable, is_identity FROM information_schema.columns"
            " WHERE table_name = 'videos' ORDER BY ordinal_position")
        self.assertEqual(columns, [
            ("id", "integer", "NO", "YES"),
            ("company_name", "text", "NO", "NO"),
            ("file_path", "text", "NO", "NO"),
            ("video_url", "text", "NO", "NO"),
            ("created_at", "timestamp with time zone", "NO", "NO"),
            ("updated_at", "timestamp with time zone", "NO", "NO"),
            ("processed_video_url", "text", "YES", "NO"),  # Internship task 2
        ])
        self.assertNotIn("bytea", [c[1] for c in columns])  # no video bytes in the database
        self.assertEqual(self.constraint_columns("PRIMARY KEY"), [("id",)])

    def constraint_columns(self, kind):
        return self.sql(
            "SELECT k.column_name FROM information_schema.table_constraints c"
            " JOIN information_schema.key_column_usage k USING (constraint_name, table_name)"
            " WHERE c.table_name = 'videos' AND c.constraint_type = %s", (kind,))

    def test_company_name_is_unique(self):
        self.assertEqual(self.constraint_columns("UNIQUE"), [("company_name",)])
        self.sql("INSERT INTO videos (company_name, file_path, video_url) VALUES ('X', 'a', 'b')")
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            self.sql("INSERT INTO videos (company_name, file_path, video_url) VALUES ('X', 'c', 'd')")

    def test_restart_keeps_the_data(self):
        self.upload("ACME", fake_video())
        web_service.create_app(self.tmp, db=SETTINGS)  # CREATE TABLE IF NOT EXISTS
        self.assertEqual([r[0] for r in self.rows()], ["ACME"])

    def test_two_processes_create_the_table_at_once(self):
        # Apache starts 2 daemon processes, both run create_app() at the same moment.
        # Without the advisory lock CREATE TABLE IF NOT EXISTS can fail in one of them.
        errors = []
        for _ in range(5):
            reset_db()
            barrier = threading.Barrier(8)

            def start():
                conn = web_service.connect(SETTINGS)
                try:
                    barrier.wait()
                    web_service.init_db(conn)
                except Exception as exc:  # noqa: BLE001 - collected and checked below
                    errors.append(repr(exc))
                finally:
                    conn.close()

            threads = [threading.Thread(target=start) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(self.sql("SELECT count(*) FROM videos")[0][0], 0)

    def test_leftover_part_files_are_removed_on_start(self):
        leftover = os.path.join(self.uploads, ".upload-abc.part")
        open(leftover, "wb").close()
        web_service.create_app(self.tmp, db=SETTINGS)
        self.assertFalse(os.path.exists(leftover))

    def test_database_down_during_a_request_is_503_json(self):
        self.app.config["DB"] = dict(SETTINGS, port=str(free_port()))
        with contextlib.redirect_stderr(io.StringIO()):
            resp = self.client.get("/videos")
        self.assertJsonError(resp, 503)
        self.assertIn("database is not available", resp.get_json()["error"])


class SettingsTest(unittest.TestCase):
    """Database settings come only from the environment."""

    def test_from_environment(self):
        env = {"DB_HOST": "db", "DB_PORT": "5432", "DB_NAME": "videos", "DB_USER": "u", "DB_PASSWORD": "p"}
        self.assertEqual(web_service.db_settings(env),
                         {"host": "db", "port": "5432", "dbname": "videos", "user": "u", "password": "p"})
        del env["DB_PORT"]
        self.assertEqual(web_service.db_settings(env)["port"], "5432")

    def test_missing_settings_are_named(self):
        with self.assertRaises(RuntimeError) as ctx:
            web_service.db_settings({"DB_HOST": "db"})
        self.assertIn("DB_NAME, DB_USER, DB_PASSWORD", str(ctx.exception))

    def test_password_is_not_in_the_description(self):
        settings = {"host": "db", "port": "5432", "dbname": "videos", "user": "u", "password": "secret"}
        self.assertEqual(web_service.describe(settings), "u@db:5432/videos")


class WaitForDatabaseTest(unittest.TestCase):
    """The service waits for the database instead of crashing on start."""

    def test_retries_until_the_database_answers(self):
        calls = []

        def flaky_connect(settings):
            calls.append(1)
            if len(calls) < 3:
                raise psycopg2.OperationalError("the database system is starting up")
            return "connection"

        settings = {"host": "db", "port": "5432", "dbname": "v", "user": "u", "password": "p"}
        with mock.patch.object(web_service, "connect", side_effect=flaky_connect), \
                mock.patch.object(web_service.time, "sleep") as sleep, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            conn = web_service.wait_for_db(settings, timeout=60)
        self.assertEqual(conn, "connection")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("waiting for the database u@db:5432/v (attempt 1)", err.getvalue())
        self.assertIn("database is ready after 3 attempts", err.getvalue())

    def test_gives_up_with_a_clear_error(self):
        settings = {"host": "127.0.0.1", "port": str(free_port()), "dbname": "videos_test",
                    "user": "u", "password": "secret"}
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.dict(os.environ, {"DB_WAIT_SECONDS": "1"}):
            with self.assertRaises(RuntimeError) as ctx:
                web_service.create_app(tmp, db=settings)
        self.assertIn("database u@127.0.0.1:", str(ctx.exception))
        self.assertIn("is not ready after 1 s", str(ctx.exception))
        self.assertNotIn("secret", str(ctx.exception))


@needs_db
class RunAsScriptTest(unittest.TestCase):
    """python web_service.py: FLASK_HOST / FLASK_PORT, uploads/ next to the script, not in the cwd."""

    def test_python_web_service_py(self):
        reset_db()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        service_dir, cwd = os.path.join(tmp, "service"), os.path.join(tmp, "elsewhere")
        os.makedirs(service_dir)
        os.makedirs(cwd)
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("web_service.py", "apply_zoom.py"):  # the service imports apply_zoom (task 2)
            shutil.copy(os.path.join(root, name), service_dir)
        port = free_port()
        env = dict(os.environ, FLASK_HOST="127.0.0.1", FLASK_PORT=str(port))
        env.update({name: SETTINGS[key] for key, name in web_service.DB_ENV.items()})
        proc = subprocess.Popen([sys.executable, os.path.join(service_dir, "web_service.py")],
                                cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    resp = requests.get(f"http://127.0.0.1:{port}/videos", timeout=1,
                                        proxies={"http": None})
                    break
                except requests.ConnectionError:
                    time.sleep(0.1)
            else:
                self.fail("the service did not start")
            self.assertEqual(resp.json(), {"videos": []})
        finally:
            proc.terminate()
            proc.wait(10)
        self.assertEqual(sql("SELECT to_regclass('videos') IS NOT NULL")[0][0], True)
        self.assertTrue(os.path.isdir(os.path.join(service_dir, "uploads")))
        self.assertEqual(os.listdir(cwd), [])


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
        self.sql("UPDATE videos SET created_at = '2000-01-01 00:00:00+00', updated_at = '2000-01-01 00:00:00+00'")
        old = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)
        row_id = self.rows()[0][3]
        new_data = fake_video()
        self.assertEqual(self.upload("ACME", new_data).status_code, 200)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], row_id)
        self.assertEqual(rows[0][4], old)
        self.assertGreater(rows[0][5], old)
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
                                          "created_at", "updated_at", "processed_video_url"})
        created = datetime.datetime.fromisoformat(videos[0]["created_at"])  # ISO 8601 text
        self.assertIsNotNone(created.tzinfo)


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
        resp = self.client.get("/upload")
        self.assertJsonError(resp, 405)
        self.assertIn("POST", resp.headers["Allow"])

    def test_file_in_use_is_503_and_keeps_the_old_video(self):
        # Windows cannot replace a file that a player is reading: os.replace raises PermissionError.
        first = fake_video()
        self.upload("ACME", first)
        with mock.patch.object(web_service.os, "replace", side_effect=PermissionError):
            resp = self.upload("ACME", fake_video())
        self.assertJsonError(resp, 503)
        self.assertEqual(self.read("ACME.webm"), first)
        self.assertEqual(self.files(), ["ACME.webm"])  # the temporary .part file is gone
        self.assertEqual(len(self.rows()), 1)

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
                    "/media/x/../../videos.db", "/media/.hidden.webm",
                    "/media/x/../.hidden.webm", "/media/x/%2e%2e/.hidden.webm"):
            with self.subTest(url):
                resp = self.get(url)
                self.assertEqual(resp.status_code, 404)
                self.assertNotIn(b"SQLite format", resp.get_data())
                self.assertTrue(resp.is_json)  # the JSON 404, not a file


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
