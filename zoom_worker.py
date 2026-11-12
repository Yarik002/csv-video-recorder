"""Zoom worker (Internship task 2): renders the zoom of uploaded videos in the background.

POST /upload with a "zoom" sidecar adds a row to the jobs table (status "pending").
This worker takes pending jobs one by one, runs apply_zoom.render() (ffmpeg) on the
uploaded file, writes uploads/zoomed/<name>-job<id>.webm and stores its URL in
PostgreSQL: jobs.processed_video_url and videos.processed_video_url.

    python zoom_worker.py          # runs until stopped (in Docker: the "worker" service)
    python zoom_worker.py --once   # processes what is pending now, then exits

Same DB_* environment variables as web_service.py. FFMPEG: path to ffmpeg if it is
not on PATH. WORKER_POLL_SECONDS: how often to look for new jobs (default 2).

Several workers can run at once: a job is taken with FOR UPDATE SKIP LOCKED, and
while it is rendered the worker holds an advisory lock on it. A job left "running"
by a worker that died (its lock is gone) goes back to "pending".
"""

import argparse
import os
import sys
import time
from urllib.parse import quote

import psycopg2

import apply_zoom
import web_service as ws

JOB_LOCK = 7270003  # pg_advisory_lock(JOB_LOCK, job id) while a job is rendered


def log(message):
    print(f"[zoom_worker] {message}", flush=True)


def recover(conn):
    """Jobs left "running" by a dead worker: nobody holds their lock, put them back."""
    with conn:
        rows = ws.fetch(conn, "SELECT id FROM jobs WHERE status = 'running'")
    for row in rows:
        with conn:
            got = ws.fetch(conn, "SELECT pg_try_advisory_lock(%s, %s) AS ok", (JOB_LOCK, row["id"]), one=True)
            if not got["ok"]:
                continue  # another worker is rendering it
            try:
                ws.fetch(conn, "UPDATE jobs SET status = 'pending', started_at = NULL"
                               " WHERE id = %s AND status = 'running' RETURNING id", (row["id"],))
                log(f"job {row['id']}: was left running by a stopped worker, queued again")
            finally:
                ws.fetch(conn, "SELECT pg_advisory_unlock(%s, %s)", (JOB_LOCK, row["id"]))


def claim(conn):
    """Takes the oldest pending job and marks it running. Returns the job with its video, or None."""
    with conn:
        row = ws.fetch(conn, """
            UPDATE jobs SET status = 'running', started_at = NOW(), finished_at = NULL, error = NULL
            WHERE id = (SELECT id FROM jobs WHERE status = 'pending' ORDER BY id
                        LIMIT 1 FOR UPDATE SKIP LOCKED)
            RETURNING id
            """, one=True)
        if row is None:
            return None
        # Session lock: stays after the commit, until this worker unlocks it or dies.
        ws.fetch(conn, "SELECT pg_advisory_lock(%s, %s)", (JOB_LOCK, row["id"]))
        return ws.fetch(conn, """
            SELECT j.id, j.video_id, j.zoom, v.company_name, v.file_path, v.video_url
            FROM jobs j JOIN videos v ON v.id = j.video_id WHERE j.id = %s
            """, (row["id"],), one=True)


def output_for(job):
    """(path, URL) of the result: uploads/zoomed/<video name>-job<id>.webm. The job id
    keeps an old render from overwriting the file of a newer one."""
    folder = os.path.join(os.path.dirname(job["file_path"]), ws.ZOOMED_DIR)
    stem = os.path.splitext(os.path.basename(job["file_path"]))[0]
    name = f"{stem}-job{job['id']}.webm"
    base = job["video_url"].rsplit("/media/", 1)[0]  # http://host:port, as the uploader saw it
    return os.path.join(folder, name), f"{base}/media/{ws.ZOOMED_DIR}/{quote(name)}"


def finish(conn, job, ok, path=None, url=None, error=None):
    with conn:
        ws.lock(conn, ws.UPLOADS_LOCK)  # the same lock as uploads: no new upload in between
        if ok:
            done = ws.fetch(conn, """
                UPDATE jobs SET status = 'done', finished_at = NOW(),
                       processed_file_path = %s, processed_video_url = %s
                WHERE id = %s AND status = 'running' RETURNING id
                """, (path, url, job["id"]), one=True)
            if done:
                ws.fetch(conn, "UPDATE videos SET processed_video_url = %s WHERE id = %s RETURNING id",
                         (url, job["video_id"]))
        else:
            done = ws.fetch(conn, """
                UPDATE jobs SET status = 'failed', finished_at = NOW(), error = %s
                WHERE id = %s AND status = 'running' RETURNING id
                """, (error, job["id"]), one=True)
    return done is not None


def process(conn, job, ffmpeg, render=apply_zoom.render):
    path, url = output_for(job)
    tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".part")
    started = time.monotonic()
    name = f"job {job['id']} ({job['company_name']})"
    try:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            config = apply_zoom.parse_sidecar(job["zoom"])
            render(job["file_path"], tmp, config, ffmpeg=ffmpeg, log=lambda m: log(f"job {job['id']}: {m}"))
            os.replace(tmp, path)
        except Exception as exc:
            message = f"{exc.__class__.__name__}: {exc}"
            log(f"{name}: failed: {message}")
            if os.path.exists(tmp):
                os.remove(tmp)
            finish(conn, job, False, error=message[:2000])
            return False
        if finish(conn, job, True, path, url):
            log(f"{name}: done in {time.monotonic() - started:.1f} s -> {url}")
            return True
        # A newer upload of this company came while rendering: this result is stale.
        os.remove(path)
        log(f"{name}: superseded by a newer upload, result dropped")
        return False
    finally:
        # Unlocked only after the status is written, so recover() never sees a
        # finished render as "running without a worker".
        with conn:
            ws.fetch(conn, "SELECT pg_advisory_unlock(%s, %s)", (JOB_LOCK, job["id"]))


def run(conn, ffmpeg, once=False, poll=2.0):
    while True:
        recover(conn)
        job = claim(conn)
        if job is not None:
            log(f"job {job['id']} ({job['company_name']}): rendering {job['file_path']}")
            process(conn, job, ffmpeg)
            continue
        if once:
            return
        time.sleep(poll)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render pending zoom jobs.")
    parser.add_argument("--once", action="store_true", help="process the pending jobs and exit")
    args = parser.parse_args(argv)

    ffmpeg = apply_zoom.find_ffmpeg(os.environ.get("FFMPEG"))
    if ffmpeg is None:
        log("ffmpeg not found: install it or set FFMPEG=<path>")
        return 2
    settings = ws.db_settings()
    poll = float(os.environ.get("WORKER_POLL_SECONDS", "2"))
    wait = float(os.environ.get("DB_WAIT_SECONDS", "60"))
    log(f"started, ffmpeg: {ffmpeg}, database: {ws.describe(settings)}")
    while True:
        conn = ws.wait_for_db(settings, wait)
        try:
            ws.init_db(conn)  # the worker may start before the web service
            run(conn, ffmpeg, once=args.once, poll=poll)
            return 0
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            # The database restarted: connect again, a dead worker's job is recovered by recover().
            log(f"database error: {str(exc).strip()}, reconnecting")
            time.sleep(poll)
        finally:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
