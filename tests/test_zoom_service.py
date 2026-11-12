"""Internship task 2: zoom jobs in the web service and zoom_worker.py.

Needs the PostgreSQL test database (see tests/db_helper.py). The worker tests run
with a fake renderer (no ffmpeg needed); one test renders for real when ffmpeg
is installed (CI installs it).

Run: python -m pytest tests/test_zoom_service.py -v
"""

import io
import json
import os
import shutil
import sys
import threading

import apply_zoom
import web_service
import zoom_worker

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db_helper import SETTINGS  # noqa: E402
from test_web_service import ServiceTestCase, fake_video  # noqa: E402

SIDECAR = {
    "viewport": {"width": 1280, "height": 720}, "fps": 25, "zoom_factor": 2.0, "ramp_sec": 0.5,
    "events": [{"t": 1.0, "x": 640, "y": 360, "type": "in"}, {"t": 2.0, "x": 640, "y": 360, "type": "out"}],
}
SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "sample.webm")


def fake_render(video, output, config, ffmpeg=None, log=print):
    """Instead of ffmpeg: copy the video and note the events."""
    shutil.copyfile(video, output)
    fake_render.calls.append((video, config["events"]))


fake_render.calls = []


class ZoomServiceTestCase(ServiceTestCase):
    def setUp(self):
        super().setUp()
        fake_render.calls = []
        self.conn = web_service.connect(SETTINGS)

    def tearDown(self):
        self.conn.close()
        super().tearDown()

    def upload_with_zoom(self, company, data=None, sidecar=SIDECAR, raw=None):
        body = raw if raw is not None else json.dumps(sidecar).encode()
        form = {
            "company_name": company,
            "file": (io.BytesIO(data or fake_video()), "clip.webm"),
            "zoom": (io.BytesIO(body), "clip.zoom.json"),
        }
        return self.client.post("/upload", data=form, content_type="multipart/form-data")

    def work(self, render=fake_render):
        """Runs the worker until the queue is empty."""
        while True:
            zoom_worker.recover(self.conn)
            job = zoom_worker.claim(self.conn)
            if job is None:
                return
            zoom_worker.process(self.conn, job, "ffmpeg", render=render)

    def job(self, job_id):
        return self.client.get(f"/jobs/{job_id}").get_json()


class ZoomJobTest(ZoomServiceTestCase):
    def test_tables(self):
        cols = [r[0] for r in self.sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'jobs' ORDER BY ordinal_position")]
        self.assertEqual(cols, ["id", "video_id", "status", "zoom", "processed_file_path", "processed_video_url",
                                "error", "created_at", "started_at", "finished_at"])

    def test_old_database_gets_the_new_column(self):
        # A database volume from Internship task 1: videos without processed_video_url, no jobs.
        self.sql("DROP TABLE jobs")
        self.sql("ALTER TABLE videos DROP COLUMN processed_video_url")
        web_service.create_app(self.tmp, db=SETTINGS)
        self.assertEqual(self.sql("SELECT count(*) FROM jobs")[0][0], 0)
        self.sql("SELECT processed_video_url FROM videos")

    def test_upload_without_zoom_is_unchanged(self):
        resp = self.upload("ACME", fake_video())
        self.assertEqual(set(resp.get_json()), {"company_name", "video_url"})
        self.assertEqual(self.sql("SELECT count(*) FROM jobs")[0][0], 0)

    def test_upload_with_zoom_queues_a_job(self):
        resp = self.upload_with_zoom("ACME GmbH")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["video_url"], "http://localhost/media/ACME_GmbH.webm")
        self.assertEqual(body["job_url"], f"http://localhost/jobs/{body['job_id']}")
        job = self.job(body["job_id"])
        self.assertEqual((job["status"], job["company_name"], job["processed_video_url"]), ("pending", "ACME GmbH", None))
        self.assertEqual(job["zoom"]["events"], SIDECAR["events"])  # stored in PostgreSQL (JSONB)

    def test_zoom_without_events_queues_nothing(self):
        resp = self.upload_with_zoom("ACME", sidecar=dict(SIDECAR, events=[]))
        self.assertNotIn("job_id", resp.get_json())

    def test_bad_zoom_is_400_and_stores_nothing(self):
        for raw in (b"{not json", json.dumps({"events": [{"t": "x", "x": 1, "y": 1}]}).encode(), b"\xff\xfe"):
            resp = self.upload_with_zoom("ACME", raw=raw)
            self.assertJsonError(resp, 400)
            self.assertIn("zoom:", resp.get_json()["error"])
        self.assertNothingStored()

    def test_worker_renders_and_stores_the_url(self):
        data = fake_video()
        job_id = self.upload_with_zoom("ACME GmbH", data).get_json()["job_id"]
        self.work()
        job = self.job(job_id)
        url = f"http://localhost/media/zoomed/ACME_GmbH-job{job_id}.webm"
        self.assertEqual((job["status"], job["processed_video_url"], job["error"]), ("done", url, None))
        self.assertEqual(self.sql("SELECT processed_video_url FROM videos")[0][0], url)
        self.assertEqual(self.client.get("/videos").get_json()["videos"][0]["processed_video_url"], url)
        self.assertEqual(fake_render.calls[0][0], os.path.join(self.uploads, "ACME_GmbH.webm"))
        # the result is served, with the video type
        resp = self.client.get(f"/media/zoomed/ACME_GmbH-job{job_id}.webm")
        self.assertEqual((resp.status_code, resp.mimetype, resp.data), (200, "video/webm", data))
        resp.close()
        self.assertEqual(os.listdir(os.path.join(self.uploads, "zoomed")), [f"ACME_GmbH-job{job_id}.webm"])

    def test_failed_render_is_stored_and_can_be_retried(self):
        def broken(*args, **kwargs):
            raise RuntimeError("ffmpeg failed (exit 1): boom")
        job_id = self.upload_with_zoom("ACME").get_json()["job_id"]
        self.work(render=broken)
        job = self.job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("boom", job["error"])
        self.assertEqual(os.listdir(os.path.join(self.uploads, "zoomed")), [])  # no .part left
        # POST /jobs/<id>/process puts it back in the queue
        resp = self.client.post(f"/jobs/{job_id}/process")
        self.assertEqual(resp.status_code, 202)
        self.assertEqual((resp.get_json()["status"], resp.get_json()["error"]), ("pending", None))
        self.work()
        self.assertEqual(self.job(job_id)["status"], "done")

    def test_process_endpoint_errors(self):
        self.assertJsonError(self.client.post("/jobs/999/process"), 404)
        self.assertJsonError(self.client.get("/jobs/999"), 404)
        self.assertEqual(self.client.get("/jobs/999/process").status_code, 405)
        job_id = self.upload_with_zoom("ACME").get_json()["job_id"]
        self.sql("UPDATE jobs SET status = 'running' WHERE id = %s", (job_id,))
        self.assertJsonError(self.client.post(f"/jobs/{job_id}/process"), 409)

    def test_reupload_supersedes_old_jobs_and_files(self):
        first = self.upload_with_zoom("ACME").get_json()["job_id"]
        self.work()
        old_file = os.path.join(self.uploads, "zoomed", f"ACME-job{first}.webm")
        self.assertTrue(os.path.exists(old_file))
        second = self.upload_with_zoom("ACME").get_json()["job_id"]
        self.assertEqual(self.job(first)["status"], "superseded")
        self.assertFalse(os.path.exists(old_file))  # the stale render is deleted
        self.assertIsNone(self.sql("SELECT processed_video_url FROM videos")[0][0])
        self.assertJsonError(self.client.post(f"/jobs/{first}/process"), 409)
        self.work()
        self.assertEqual(self.job(second)["status"], "done")

    def test_upload_during_render_drops_the_stale_result(self):
        job_id = self.upload_with_zoom("ACME").get_json()["job_id"]

        def slow(video, output, config, ffmpeg=None, log=print):
            fake_render(video, output, config)
            self.upload("ACME", fake_video())  # a new video arrives meanwhile
        self.work(render=slow)
        self.assertEqual(self.job(job_id)["status"], "superseded")
        self.assertIsNone(self.sql("SELECT processed_video_url FROM videos")[0][0])
        self.assertEqual(os.listdir(os.path.join(self.uploads, "zoomed")), [])

    def test_job_of_a_dead_worker_is_recovered(self):
        job_id = self.upload_with_zoom("ACME").get_json()["job_id"]
        # A worker took it and died: status running, nobody holds the job lock.
        self.sql("UPDATE jobs SET status = 'running' WHERE id = %s", (job_id,))
        self.work()
        self.assertEqual(self.job(job_id)["status"], "done")

    def test_running_job_of_a_live_worker_is_left_alone(self):
        job_id = self.upload_with_zoom("ACME").get_json()["job_id"]
        other = web_service.connect(SETTINGS)  # "another worker"
        try:
            job = zoom_worker.claim(other)
            self.assertEqual(job["id"], job_id)
            zoom_worker.recover(self.conn)
            self.assertEqual(self.job(job_id)["status"], "running")
            self.assertIsNone(zoom_worker.claim(self.conn))  # nothing else to take
        finally:
            other.close()  # the session lock goes away with the connection

    def test_two_workers_never_take_the_same_job(self):
        ids = [self.upload_with_zoom(f"Company {i}").get_json()["job_id"] for i in range(6)]
        taken, lock = [], threading.Lock()

        def worker():
            conn = web_service.connect(SETTINGS)
            try:
                while True:
                    job = zoom_worker.claim(conn)
                    if job is None:
                        return
                    with lock:
                        taken.append(job["id"])
                    zoom_worker.process(conn, job, "ffmpeg", render=fake_render)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(sorted(taken), sorted(ids))
        self.assertEqual({self.job(i)["status"] for i in ids}, {"done"})

    def test_render_with_real_ffmpeg(self):
        ffmpeg = apply_zoom.find_ffmpeg()
        if ffmpeg is None:
            self.skipTest("ffmpeg is not installed")
        with open(SAMPLE, "rb") as f:
            data = f.read()
        job_id = self.upload_with_zoom("ACME", data).get_json()["job_id"]
        job = zoom_worker.claim(self.conn)
        self.assertTrue(zoom_worker.process(self.conn, job, ffmpeg))
        path = os.path.join(self.uploads, "zoomed", f"ACME-job{job_id}.webm")
        self.assertEqual(apply_zoom.probe_size(ffmpeg, path), (1280, 720))
        self.assertEqual(self.job(job_id)["status"], "done")
