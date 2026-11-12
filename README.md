# CSV video recorder + video web service

[![CI](https://github.com/Yarik002/csv-video-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/Yarik002/csv-video-recorder/actions/workflows/ci.yml)

One repository, four steps:

- **Internship task 2** (latest, start here): Camtasia-style zoom. F5 (or `window.__zoomAt(x, y)` from the scenario) marks a zoom at the mouse point while recording, the events go to `<video>.zoom.json`, `apply_zoom.py` renders a smooth zoom with ffmpeg `zoompan`, and in Docker a `worker` container does it for every uploaded video and stores the result URL in PostgreSQL. How to run it is right below.
- **Internship task 1**: the video web service runs in Docker Compose: Apache + mod_wsgi with the Flask service in one container, PostgreSQL in another, see [Internship task 1: how to run](#internship-task-1-how-to-run). The version I sent is the tag [`internship-1`](https://github.com/Yarik002/csv-video-recorder/tree/internship-1).
- **Task 2**: the Flask web service (upload, look up by company, play) and the recorder's `--server` option, see [Task 2: video web service](#task-2-video-web-service). The version I sent for Task 2 (still on SQLite) is the tag [`task-2`](https://github.com/Yarik002/csv-video-recorder/tree/task-2).
- **Task 1**: the CSV-driven browser recorder, see [Task 1: CSV-driven recorder](#task-1-csv-driven-recorder). The version I sent is the tag [`task-1`](https://github.com/Yarik002/csv-video-recorder/tree/task-1).

## Internship task 2: how to run

### 1. The Docker stack (web + worker + db)

Same as Internship task 1, now with a third container `worker` (same image, ffmpeg inside). Windows:

```bat
git clone https://github.com/Yarik002/csv-video-recorder.git
cd csv-video-recorder
copy .env.example .env
notepad .env
docker compose up --build -d --wait
```

(Linux/macOS: `cp .env.example .env`, `nano .env`.) Set your own `DB_PASSWORD` in `.env`. An existing database volume from task 1 is upgraded on start (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS jobs`).

Upload a video together with its zoom events (PowerShell: `curl.exe`):

```bash
curl -X POST http://localhost:8000/upload -F "company_name=ACME GmbH" -F "file=@docs/sample.webm" -F "zoom=@docs/sample.zoom.json"
curl http://localhost:8000/jobs/1
```

Answers from a real run (the second one after about 40 s, the worker needs about as long as the video):

```
{"company_name":"ACME GmbH","job_id":1,"job_url":"http://localhost:8000/jobs/1","video_url":"http://localhost:8000/media/ACME_GmbH.webm"}
{"company_name":"ACME GmbH","error":null,"finished_at":"2026-11-12T20:59:20.322859+00:00","id":1,"processed_video_url":"http://localhost:8000/media/zoomed/ACME_GmbH-job1.webm","started_at":"2026-11-12T20:58:41.361573+00:00","status":"done","video_id":5,"zoom":{...}}
```

Open `processed_video_url` in a browser: the zoomed video. `docker compose logs -f worker` shows the jobs. `GET /videos` has `processed_video_url` for every company.

### 2. Recording with zoom (on your computer)

Setup of the recorder as in [Task 2: how to run the recorder](#task-2-how-to-run-the-recorder) (venv, `pip install -r requirements.txt`, `python -m playwright install chromium`). Then:

```bash
python capture_video.py --zoom --server http://localhost:8000
```

- `--zoom`: the sample scenario zooms in on the search box while it types the query (`window.__zoomAt(x, y)`, the automation does not press F5).
- **F5** during any recording toggles the zoom by hand: the first press starts the zoom-in at the mouse, the second starts the zoom-out. A green ring shows "zoom in", a red ring "zoom out". The page is not reloaded.
- After each video with at least one event, `video/<Company>.zoom.json` is written next to it, and `--server` uploads it with the video. The worker renders it, `GET /videos` shows the result.

### 3. apply_zoom.py by hand

```bash
python apply_zoom.py --video video/ACME_GmbH.webm             # -> video/ACME_GmbH.zoomed.webm
python apply_zoom.py --video video/ACME_GmbH.webm --dry-run   # print the ffmpeg command only
python apply_zoom.py --video video/ACME_GmbH.webm --ffmpeg C:\ffmpeg\bin\ffmpeg.exe
```

It needs an ffmpeg with `zoompan` and `libvpx-vp9` (the one Playwright downloads has neither). If you have no ffmpeg on Windows, run it in the container, the `video` folder mounted:

```bash
docker compose run --rm -v "%cd%/video:/video" worker /opt/venv/bin/python /app/apply_zoom.py --video /video/ACME_GmbH.webm
```

(Linux/macOS: `"$(pwd)/video:/video"`.)

### Acceptance checklist

Checked on Windows 11 with Docker Desktop on 12-13 November 2026, and in CI.

| # | Item | How it was checked |
|---|---|---|
| 1 | The first F5 marks the start of the zoom-in, the second the start of the zoom-out | test with Playwright's F5 (`tests/test_zoom_capture.py`), and a real F5 from Windows (`keybd_event`) in a headed Chromium: two events `in`/`out` at the exact mouse position, the page was not reloaded |
| 2 | `window.__zoomAt(x, y)` toggles in/out from the scenario | tests; `capture_video.py --zoom` on the live DuckDuckGo, 3 of 3 videos with an in/out pair around the typed query |
| 3 | `<video>.zoom.json` is written only when there is at least one event | tests: no events, no file (and an old file of the same company is removed); the existing recorder test still sees only `.webm` files |
| 4 | `python apply_zoom.py --video ...` produces a zoomed `.webm` | tests with ffmpeg (VP9, same 1280x720); by hand in the container on a live recording |
| 5 | `--dry-run` prints the ffmpeg command without encoding | test: works even with a non-existent `--ffmpeg`, no file is created |
| 6 | An open-ended zoom holds to the end | tests: the math, and a real render where the last frame is still zoomed |
| 7 | The zoom centres on the exact mouse point and stays within the frame | real render of a red square at (900, 200): in the hold it is at (640, 360) and twice as big; near a corner the crop sticks to the edges |
| 8 | Smooth (ease-in, hold, ease-out), several pairs | tests: smoothstep ramp frame by frame, 2 pairs, overlapping pairs moved; frames of real renders looked at by eye |
| 9 | Post-processing runs in the Docker stack and stores the result in PostgreSQL | `curl` upload with `zoom=@docs/sample.zoom.json`, the `worker` container rendered it, `jobs` and `videos.processed_video_url` filled, Apache serves the file (200 `video/webm`, Range 206); live run: 3 recordings rendered by the worker; CI job `docker` does the same and checks it after `down`/`up` |
| 10 | Tests pass locally and in CI | `python -m pytest tests`: 112 passed, 5 skipped (the ffmpeg tests: no ffmpeg on my Windows, they passed in a container with ffmpeg, 39 of 39 in `tests/test_zoom.py`); CI installs ffmpeg: 117 passed on Python 3.9, 3.12, 3.14, and the `docker` job is green |

### Internship task 2: how it works

```
 recording (qscript.py)                    post-processing (apply_zoom.py, ffmpeg zoompan)
 F5 / window.__zoomAt(x, y)                 sidecar -> pairs in/out -> smoothstep ramp ->
   -> {t, x, y, type} -> <video>.zoom.json   crop iw/zoom x ih/zoom centred on the point -> .webm

 Docker: POST /upload (file + zoom) -> row in "jobs" (pending) -> worker container takes it
         (FOR UPDATE SKIP LOCKED) -> uploads/zoomed/<name>-job<id>.webm -> processed_video_url
```

| File | What it does |
|---|---|
| `qscript.py` | Changed: `ZOOM_JS` (F5, `window.__zoomAt`, green/red ring, sync marker), `ZoomRecorder` (state, events, sidecar), the scenario zooms on the search box with `ZOOM_IN_SCENARIO` |
| `capture_video.py` | Changed: `--zoom` |
| `apply_zoom.py` | New: sidecar parsing, pairing, easing, crop math, ffmpeg filter, `--dry-run`, `--ffmpeg`, sync calibration |
| `zoom_worker.py` | New: the worker (queue in PostgreSQL) |
| `web_service.py` | Changed: optional `zoom` field in `POST /upload`, table `jobs`, column `videos.processed_video_url`, `GET /jobs/<id>`, `POST /jobs/<id>/process`, `GET /media/zoomed/<file>` |
| `video_client.py` | Changed: `UploadVideo` sends `<video>.zoom.json` with the video when it exists (signature unchanged) |
| `Dockerfile`, `docker-compose.yml` | ffmpeg in the image, service `worker` |
| `docs/sample.zoom.json` | Zoom events for `docs/sample.webm` (search box, then the first result) |
| `tests/test_zoom.py`, `tests/test_zoom_capture.py`, `tests/test_zoom_service.py` | New pytest tests |

Decisions worth knowing:
- **Times come from the page.** Playwright's sync API handles calls from the page only while Python calls Playwright; the scenario sleeps a lot. The page sends `Date.now()` of the key press, so a late call does not shift the event.
- **Sync marker.** The video starts 0.7-1.8 s after the context is created, different every run (measured, see `docs/NOTES.md`). So `t` is counted from the context, and a 12x12 magenta square in the top-left corner disappears 3 s after it. `apply_zoom.py` finds that frame in the video and moves the events by the difference; measured error after that: 0.09 s at most. The square is visible in the first seconds of every recording.
- **Easing.** Smoothstep `p*p*(3-2*p)`, ramps of `ramp_sec` (0.5 s). The zoom-out starts at the `out` event. A pair that starts before the previous zoom-out has finished is moved after it.
- **Worker, not a request.** Rendering takes about as long as the video, so the upload answers at once and the `worker` container renders in the background. Several workers can run: `FOR UPDATE SKIP LOCKED` and an advisory lock per job; a job of a worker that died goes back to `pending`. A new upload of the same company supersedes its old jobs and deletes the old zoomed file.
- **Old sidecars without `type`** are read as toggles (in, out, in, ...).

Before sending I cloned the repository from GitHub into an empty folder and followed this section: fresh volumes, `docker compose up --build -d --wait`, the `curl` upload with `zoom=`, the worker rendered it in 49 s, the result 200 `video/webm`; the tests in a new Python 3.14 venv: 112 passed, 5 skipped (no ffmpeg on Windows).

Limits: the 2x zoom of a 1280x720 recording is an upscale, so it is softer than the original. The sync square is in the video. Rendering is about real time on my laptop (36.8 s sample in 39 s).

## Internship task 1: how to run

You need Docker with Docker Compose v2 (Docker Desktop on Windows and macOS). Nothing else: Python, Apache and PostgreSQL are inside the containers.

Windows (cmd or PowerShell):

```bat
git clone https://github.com/Yarik002/csv-video-recorder.git
cd csv-video-recorder
copy .env.example .env
notepad .env
docker compose up --build
```

Linux or macOS:

```bash
git clone https://github.com/Yarik002/csv-video-recorder.git
cd csv-video-recorder
cp .env.example .env
nano .env
docker compose up --build
```

In `.env` set your own `DB_PASSWORD`. `.env` is in `.gitignore`, so the password never gets into git; without it Compose stops with `DB_PASSWORD is not set, copy .env.example to .env`.

The first build takes a few minutes (6.5 minutes on my laptop: it compiles mod_wsgi and psycopg2), later starts take about 15 seconds. Ready when the log shows:

```
db-1   | ... LOG:  database system is ready to accept connections
web-1  | ... AH00489: Apache/2.4.68 (Debian) mod_wsgi/6.1.1 Python/3.12 configured -- resuming normal operations
```

`docker compose up --build -d --wait` does the same in the background and returns when both containers are healthy. The service is at http://localhost:8000.

Check it from a second terminal (in PowerShell write `curl.exe`, plain `curl` there is another command):

```bash
curl -X POST http://localhost:8000/upload -F "company_name=ACME GmbH" -F "file=@docs/sample.webm"
curl "http://localhost:8000/get_video_url?company_name=ACME%20GmbH"
curl http://localhost:8000/videos
```

Answers from a real run:

```
{"company_name":"ACME GmbH","video_url":"http://localhost:8000/media/ACME_GmbH.webm"}
{"company_name":"ACME GmbH","video_url":"http://localhost:8000/media/ACME_GmbH.webm"}
{"videos":[{"company_name":"ACME GmbH","created_at":"2026-11-09T12:26:16.553708+00:00","file_path":"/app/uploads/ACME_GmbH.webm","id":1,"updated_at":"2026-11-09T12:26:16.553708+00:00","video_url":"http://localhost:8000/media/ACME_GmbH.webm"}]}
```

Open the `video_url` in a browser: it plays. Then stop and start again, the data stays:

```bash
docker compose down
docker compose up -d --wait
curl "http://localhost:8000/get_video_url?company_name=ACME%20GmbH"
```

The same check as a script (it is also what CI runs): `python scripts/docker_smoke.py upload`, then `docker compose down`, `docker compose up -d --wait`, `python scripts/docker_smoke.py check`. It needs only `requests`.

Useful commands:

```bash
docker compose logs -f web                         # Apache access and error log, messages of the service
docker compose exec web curl http://localhost/videos
docker compose exec db psql -U video_user -d videos -c "SELECT id, company_name, video_url FROM videos"
docker compose down -v                             # stop and DELETE the data (volumes)
```

The recorder from Task 2 works with the new server as before, only the port is the Docker one (setup of the recorder: [Task 2: how to run the recorder](#task-2-how-to-run-the-recorder)):

```bash
python capture_video.py --server http://localhost:8000
```

### Acceptance checklist

Checked on Windows 11 with Docker Desktop (Docker 29.8.2, Compose 5.6.0) on 9 November 2026, and on every push in CI (job `docker`, Ubuntu 24.04).

| # | Item | How it was checked |
|---|---|---|
| 1 | `docker compose up --build` starts both containers without errors | from empty volumes: both `healthy` in 15 s (image already built); CI builds from scratch |
| 2 | `POST /upload` uploads a video and returns a working URL | `curl` and `scripts/docker_smoke.py`: 200, URL `http://localhost:8000/media/ACME_GmbH.webm`; the URL gives 200, `video/webm`, the bytes are identical to the upload, a Range request gives 206; it plays in Chromium |
| 3 | `GET /get_video_url?company_name=...` returns the URL | `curl`, smoke script, `video_client.GetVideoURL` |
| 4 | After `docker compose down` and `docker compose up` data and videos are preserved | `down` (without `-v`), `up`: the same row and a byte-identical video; smoke script `check` |
| 5 | `web` waits for `db` and does not crash on startup | see [Waiting for the database](#waiting-for-the-database): `depends_on: service_healthy`, plus the service itself retries; tested with `db` stopped |

Also checked by hand: `python capture_video.py --server http://localhost:8000` on the live DuckDuckGo, all 3 rows of `input.csv` recorded, uploaded to the Docker service and played in the preview window (the Manchester row needed two retries: DuckDuckGo did not open within 30 s on my slow network, before the service was involved). The 57 tests pass locally against `postgres:16` and in CI on Python 3.9, 3.12, 3.14. Before sending I cloned the repository from GitHub into an empty folder and followed this README: both containers healthy, the `curl` checks, `down`/`up`, and the tests in a new Python 3.14 venv (57 OK).

## Internship task 1: how it works

```
 browser / video_client.py / capture_video.py --server
              |
              | http://localhost:8000
              v
 +------------------- web ---------------------+        +------- db -------+
 | Apache :80                                  |        | postgres:16      |
 |   /media/*  -> files in /app/uploads (Alias)|  SQL   |   table videos   |
 |   /*        -> mod_wsgi -> Flask app  ------+------->|                  |
 +---------------------------------------------+  :5432 +------------------+
         volume "uploads" (/app/uploads)                volume "db_data"
```

### Files

| File | What it does |
|---|---|
| `Dockerfile` | Image of `web`: `python:3.12-slim-bookworm` + Debian Apache 2.4 + a venv with Flask, psycopg2 and mod_wsgi. Two stages: compilers stay in the first one |
| `docker-compose.yml` | Services `web` and `db`, port `8000:80`, named volumes `db_data` and `uploads`, healthchecks, `depends_on: condition: service_healthy` |
| `.env.example` | Template for `.env` with `DB_NAME`, `DB_USER`, `DB_PASSWORD` |
| `apache/video-service.conf` | The Apache VirtualHost: `WSGIDaemonProcess`, `WSGIScriptAlias`, `Alias /media/` |
| `web_service.wsgi` | Entry point for mod_wsgi: `application = create_app()` |
| `docker/apache-foreground.sh` | Starts Apache in the foreground as the main process of the container |
| `web_service.py` | Changed: PostgreSQL (psycopg2) instead of SQLite, settings from the environment, waits for the database |
| `requirements.txt` | Changed: `psycopg2-binary==2.9.12` added (for the client, the tests and `python web_service.py` on your computer) |
| `requirements-docker.txt` | New: what goes into the image, `flask==3.1.3`, `psycopg2==2.9.12`, `mod-wsgi==6.1.1` |
| `scripts/docker_smoke.py` | Checks the running containers from outside with `video_client` |
| `tests/db_helper.py` | PostgreSQL for the tests |

### Apache and mod_wsgi

mod_wsgi has to be built for the same Python that runs the app, otherwise Apache loads a module for another Python. The image has Python 3.12, and `pip install mod-wsgi` builds the module for exactly this Python (against Debian's `apache2-dev`). `mod_wsgi-express module-config` then prints the line Apache needs, it goes to `/etc/apache2/mods-available/wsgi.load`:

```
LoadModule wsgi_module "/opt/venv/lib/python3.12/site-packages/mod_wsgi/server/mod_wsgi-py312.cpython-312-x86_64-linux-gnu.so"
WSGIPythonHome "/opt/venv"
```

The system package `libapache2-mod-wsgi-py3` would be built for Debian's own Python 3.11, not for the 3.12 of the image, so I did not use it.

The VirtualHost (`apache/video-service.conf`):
- `WSGIDaemonProcess video-service processes=2 threads=15`: the Flask app runs in 2 separate processes with 15 threads each, as user `www-data`, not inside the Apache workers.
- `WSGIScriptAlias / /app/web_service.wsgi process-group=... application-group=...`: with both options mod_wsgi loads the app when the process starts, not on the first request. So the app connects to the database and creates the table before it answers anything.
- `Alias /media/ /app/uploads/`: Apache sends the videos itself (Range requests, no Python in between), as the task suggests for production. Files starting with a dot (unfinished uploads) are denied, directory listing is off. The Flask route `/media/<filename>` stays for running without Docker.
- Both logs go to the container output: `docker compose logs web`. Healthcheck requests are not logged.

### SQLite to PostgreSQL

| | Task 2 (SQLite) | Now (PostgreSQL) |
|---|---|---|
| Connection | `sqlite3.connect("videos.db")` | `psycopg2.connect(host, port, dbname, user, password)` from `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` |
| Placeholders | `?` | `%s` |
| Id | `INTEGER PRIMARY KEY AUTOINCREMENT` | `INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY` |
| Time | `TEXT DEFAULT (datetime('now'))` | `TIMESTAMPTZ DEFAULT NOW()`, in JSON as ISO 8601 (`2026-11-09T12:26:16.553708+00:00`) |
| Upsert | `ON CONFLICT(company_name) DO UPDATE` | the same, `UNIQUE` on `company_name` stays |
| Rows as dicts | `sqlite3.Row` | `RealDictCursor` |
| Per request | connection in `flask.g` | the same: a new connection per request, closed in `teardown_appcontext` |
| One upload at a time | `BEGIN IMMEDIATE` (SQLite write lock) | `pg_advisory_xact_lock` (see below) |
| Case-insensitive file check | `COLLATE NOCASE` | `lower(file_path) = lower(%s)` |

The table:

```sql
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_name TEXT        NOT NULL UNIQUE,
    file_path    TEXT        NOT NULL,  -- absolute path to the file on disk
    video_url    TEXT        NOT NULL,  -- public URL to the video
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
```

### Waiting for the database

Two layers, because one is not enough:
1. **Compose.** `web` has `depends_on: db: condition: service_healthy`, and the `db` healthcheck is `pg_isready -h 127.0.0.1 ...`. The `-h 127.0.0.1` matters: on the very first start the postgres image runs a temporary server on a Unix socket only, while it creates the database, then restarts it. I polled both variants every 50 ms during a first start: the socket check said "ready" on the temporary server about 1.3 s before the real one was up, the TCP check waited for the real one.
2. **The service.** `create_app()` connects with retries once a second for up to `DB_WAIT_SECONDS` (60) and logs every attempt. This was needed for real: after Docker Desktop restarted, both containers were started at the same moment by `restart: unless-stopped` (then `depends_on` does not apply), and the log shows `waiting for the database ... (attempt 1..4): the database system is starting up`, then `database is ready after 5 attempts`.

What I tried by hand: `db` stopped, `web` restarted: the service logs the attempts, requests wait, and as soon as `db` is started the requests are answered. If `db` stays away longer than 60 s, the service logs `RuntimeError: database video_user@db:5432/videos is not ready after 60 s: could not translate host name "db" ...`, Apache keeps running and the container is not restarted in a loop; the next request tries again, and after `db` came back it got 200. If the database goes away while the service is running, requests get `503 {"error": "database is not available, try again"}` instead of a 500 (checked by a test).

### Decisions worth knowing

- **Advisory locks instead of `BEGIN IMMEDIATE`.** Choosing the file name, moving the file and writing the row must be one step, also across the 2 Apache processes. `SELECT pg_advisory_xact_lock(key)` takes a lock in PostgreSQL that every process sees and that ends with the transaction. The same for `CREATE TABLE IF NOT EXISTS`: two processes start at once, and without a lock PostgreSQL can fail with `duplicate key value violates unique constraint "pg_class_relname_nsp_index"`. I removed each lock for a moment and ran the tests: both tests failed, with the locks they pass.
- **Secrets.** The password is only in `.env` (not in git). Compose passes it to both containers. The CI job generates a random one.
- **No database port on the host.** `db` has no `ports`, only `web` reaches it inside the Compose network by the name `db`.
- **psycopg2 in two forms.** In the image it is built from source against `libpq` (`psycopg2`), as the psycopg2 docs recommend for production. On your computer `psycopg2-binary` installs without a compiler, also on Windows. Version 2.9.12, the last with Python 3.9 wheels.
- **Graceful stop.** `STOPSIGNAL SIGWINCH`: on `docker compose down` Apache finishes the requests in progress (an upload) and exits.
- **Restarts.** Both services have `restart: unless-stopped`.

### Limits

- `/media/` is now served by Apache, so a missing video gives Apache's HTML 404, not the JSON 404 of Flask. The JSON API endpoints are unchanged.
- The image is 358 MB (Python 3.12 slim + Apache).
- There is no HTTPS and no login: the task does not ask for them. In production the password would go into Docker secrets rather than an environment variable.
- `.env` lives next to `docker-compose.yml`; `docker compose` must be run from this folder.

### Tests and CI

Tests need a PostgreSQL. Locally I start a throwaway one in Docker on port 5433 (the test database name must end with `_test`, the tests drop the table):

```bash
docker run -d --name videos-test-db -p 127.0.0.1:5433:5432 -e POSTGRES_DB=videos_test -e POSTGRES_USER=video_user -e POSTGRES_PASSWORD=test_only postgres:16
```

Then, in the venv (see [Task 2: how to run the recorder](#task-2-how-to-run-the-recorder)), PowerShell:

```powershell
$env:DB_HOST="127.0.0.1"; $env:DB_PORT="5433"; $env:DB_NAME="videos_test"; $env:DB_USER="video_user"; $env:DB_PASSWORD="test_only"
python -m pytest tests -v
```

Linux/macOS: `DB_HOST=127.0.0.1 DB_PORT=5433 DB_NAME=videos_test DB_USER=video_user DB_PASSWORD=test_only python -m pytest tests -v`.

Without these variables the database tests are skipped with the reason. 57 tests, about 2 minutes, new for this task: the exact PostgreSQL schema (identity key, `UNIQUE`, `timestamptz`, no `bytea`), 8 processes creating the table at once (5 rounds), restart keeps the data, settings only from the environment and no password in errors, waiting for the database (retries, clear error after the timeout), 503 when the database is down, and all Task 2 tests (20 parallel uploads now check the advisory lock).

Internship task 2 switched the runner to pytest (`pytest==8.4.2`, the last for Python 3.9; pytest also runs the older unittest classes) and added 60 tests: zoom math and sidecar parsing, real ffmpeg renders (skipped without ffmpeg, CI installs it), F5 and `__zoomAt` in Chromium, the jobs table and the worker. Now 112 tests plus 5 that need ffmpeg, about 4 minutes.

CI (`.github/workflows/ci.yml`) has two jobs:
- `test`: the tests on Python 3.9, 3.12, 3.14 with `postgres:16` as a GitHub Actions service; `REQUIRE_DB=1` makes them fail instead of skip if the database is missing.
- `docker`: `.env` with a random password, `docker compose up --build --wait`, `scripts/docker_smoke.py upload`, `docker compose down`, `up`, `scripts/docker_smoke.py check`, logs.

## Task 2: video web service

The service from Task 2. Since Internship task 1 it uses PostgreSQL; the SQLite version is the tag [`task-2`](https://github.com/Yarik002/csv-video-recorder/tree/task-2).

### Task 2: how to run the recorder

Python 3.9 or newer. Install once.

Windows (cmd or PowerShell):

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

If PowerShell says that running scripts is disabled, run `Set-ExecutionPolicy -Scope Process Bypass` first, or use cmd.

Linux or macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

Terminal 1, the service: `docker compose up --build` (see above). Without Docker, `python web_service.py` runs the same app on the Flask dev server at `0.0.0.0:8000` (`FLASK_HOST`, `FLASK_PORT`), but it needs a PostgreSQL and the `DB_*` variables.

Terminal 2, record the 3 rows of `input.csv`, upload each video and open it in Chromium:

```bash
python capture_video.py --server http://localhost:8000
```

Output of a real run against the Docker service (9 November 2026):

```
[1/3] Acme Retail GmbH (Berlin)
  saved: C:\...\csv-video-recorder\video\Acme Retail GmbH.webm (54 s)
  uploaded: http://localhost:8000/media/Acme_Retail_GmbH.webm
[2/3] Nordic Home; Garden AB (Stockholm)
  saved: C:\...\csv-video-recorder\video\Nordic Home; Garden AB.webm (38 s)
  uploaded: http://localhost:8000/media/Nordic_Home_Garden_AB.webm
```

The recorder needs a visible browser window: DuckDuckGo answers headless browsers with an error page (see [Headless](#headless)).

After each upload a Chromium window opens the returned URL and its built-in player plays the video for 10 seconds (`--view-seconds N`; `--view-seconds 0` keeps it open until you press Enter):

![The preview window: Chromium plays the uploaded video from the service](docs/img/preview.png)

### Endpoints

| Method and path | Answer |
|---|---|
| `POST /upload` (multipart: `file`, `company_name`) | 200 `{"company_name": "...", "video_url": "http://<host>:<port>/media/<file>"}` |
| `GET /get_video_url?company_name=<name>` | 200 the same JSON; 404 `{"error": "company not found", ...}` |
| `GET /media/<filename>` | the video file, `Content-Type: video/webm`, Range requests answered with 206, so the player can seek |
| `GET /videos` | `{"videos": [{id, company_name, file_path, video_url, created_at, updated_at}, ...]}`, sorted by name |

Every error of the API is JSON with an `error` field: 400 for a missing or empty `file`, a missing or blank `company_name`, an extension that is not `.webm`, `.mp4`, `.ogv` or `.ogg` (a file without an extension is taken as `.webm`); 404; 405 for a wrong method; 413 for a file over 500 MB; 503 if the database is not available, or (on Windows without Docker) if the file is being read at that moment. Spaces around `company_name` are removed.

### Files of Task 2

| File | What it does |
|---|---|
| `web_service.py` | The Flask service and the database layer (`create_app`, `init_db`, `get_db`, `upsert_video`, `find_video`, `list_videos`) |
| `video_client.py` | `UploadVideo(server_url, video_path, company_name) -> str` and `GetVideoURL(server_url, company_name) -> str` with the exact signatures, plus `PreviewVideo(video_url, view_seconds)` |
| `capture_video.py` | `--server URL` and `--view-seconds N`. Without `--server` it works and prints exactly as in Task 1 |
| `qscript.py` | Not changed (Task 1) |
| `tests/test_web_service.py`, `tests/test_upload_preview.py` | Tests for the service and the client |

### How the hints of Task 2 are done

- **Connection per request** in `flask.g`, closed in `teardown_appcontext`. No connection is shared between threads.
- **URL from `request.host_url`.** A request to `http://localhost:8000` gives `http://localhost:8000/media/...`, a request from another computer to `http://192.168.1.5:8000` gives that address. Behind Apache this works the same, Apache passes the `Host` header.
- **`send_from_directory`** serves `uploads/` without Docker (refuses paths outside the folder, answers Range requests); in Docker Apache does it.
- **Path and URL in the DB, not bytes.** Tested: no binary column, the file on disk is byte-identical to the upload.
- **Multipart from the client.** `requests` with `files={"file": (filename, fileobj, "video/webm")}, data={"company_name": ...}`.
- **Direct `.webm` URL in Chromium** shows its built-in player; `PreviewVideo` launches Chromium with `headless=False` and goes to the URL.
- **Lazy import.** `capture_video.py` imports `video_client` only when `--server` is given, and `video_client` imports `requests` and Playwright inside the functions.

### Decisions of Task 2

- **File names.** `ACME GmbH` becomes `ACME_GmbH.webm`: every character except `A-Z a-z 0-9 . _ -` becomes `_` (my own small rule, not `werkzeug.utils.secure_filename`: Task 2 allowed only flask, requests and playwright). Different companies can give the same name (`A B` and `A_B`; `acme` and `ACME` on Windows), so the service checks in the database whether another company already uses this file and then adds the first 8 characters of the SHA-1 of the company name (`A_B_2429e5cb.webm`). Names with no Latin letters or digits (`ООО Ромашка`) and Windows device names (`CON`) get `video_<hash>`.
- **Atomic save.** The upload is written to a temporary `.part` file in `uploads/`, then moved over the old file with `os.replace`, so a player never gets half a file.
- **A file that is being played (Windows, without Docker).** An open file cannot be replaced there; the service retries for 0.8 s, then answers 503.
- **Errors in the batch.** A failed upload prints `upload failed: <reason>`, a failed preview prints `preview failed: <reason>`, and the loop goes on with the next row. The exit code is 1 if any of them failed.

### Limits of Task 2

- The URL is saved at upload time, as the schema says. If the service is later opened under another address, old rows keep the old host until the video is uploaded again.
- The host in `video_url` comes from the request, as the task says, so a client decides which host is saved. `/videos` shows absolute paths on the server. Fine for an internal tool, not for the open internet.
- There is no login, no delete endpoint and no HTML page: the task does not ask for them.

## Task 1: CSV-driven recorder

This part is unchanged since Task 1 (except the `--server` option, see Task 2). Python + Playwright (sync API, Chromium). `capture_video.py` reads a CSV and, for every row, calls `qscript.CaptureVideo_Magento(...)`. The function opens the page, shows an SVG cursor with a yellow highlight, glides to the search box, types `Magento developer in <city>` like a person, clicks the search button and then the first result. Every click shows a ripple. Playwright records it all into `video/<company_name>.webm`.

![Frames from a real recording](docs/img/frames.png)

A full recording made by this code: [docs/sample.webm](docs/sample.webm?raw=true) (37 s, 2.3 MB). GitHub does not play `.webm` on the page: download it and open it in a browser or any video player.

### Run

Python 3.9 or newer. On Windows:

```bash
git clone https://github.com/Yarik002/csv-video-recorder.git
cd csv-video-recorder
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
python capture_video.py
```

On Linux or macOS the same, with `python3 -m venv .venv` and `source .venv/bin/activate`.

Output:

```
[1/3] Acme Retail GmbH (Berlin)
  saved: C:\...\csv-video-recorder\video\Acme Retail GmbH.webm (32 s)
[2/3] Nordic Home; Garden AB (Stockholm)
  saved: C:\...\csv-video-recorder\video\Nordic Home; Garden AB.webm (33 s)
[3/3] Atlantic Supplies Ltd (Manchester)
  saved: C:\...\csv-video-recorder\video\Atlantic Supplies Ltd.webm (33 s)
Done: 3 of 3 videos saved.
```

A browser window opens for each row; this is on purpose, see "Headless" below. Options:

```bash
python capture_video.py other.csv --video-dir out    # another CSV and output folder
python capture_video.py --headless                   # no window (DuckDuckGo blocks it)
```

### Files

| File | What it does |
|---|---|
| `qscript.py` | `CaptureVideo_Magento(...)` with the exact signature, the global `PATH_TO_VIDEO_FOLDER = "./video"`, the sample behaviour, the injected cursor/highlight/ripple/callout and the `ShowCallout` helper |
| `capture_video.py` | Reads the CSV with `csv.DictReader(f, delimiter=";", quotechar='"')` and calls the function for every row, prints the saved path. Exit code 1 if any row failed |
| `input.csv` | Sample input, 3 rows. The second company name contains `;` inside quotes to show the quote handling |
| `requirements.txt` | `playwright` (Task 2 added `flask` and `requests`, Internship task 1 `psycopg2-binary`) |
| `tests/` | Offline tests on a local copy of the search flow, run in CI on Python 3.9, 3.12 and 3.14 |
| `docs/NOTES.md` | Problems I ran into and how I solved them |

### How it works

**Visual effects.** One JS snippet is added with `context.add_init_script` (a plain IIFE, so it runs on every page, including the results page and the opened site). It creates a 32×32 SVG arrow (white fill, black outline), a semi-transparent yellow circle with radius 32 px centred on the pointer and placed behind the arrow, and listens to `mousemove`. All elements have `pointer-events: none` and the top `z-index`, so they never take clicks. Styles are set through `element.style` and animations through `element.animate`, because sites with a strict CSP block injected `<style>` tags; the SVG is built with `createElementNS`, because `innerHTML` is blocked on sites with Trusted Types. The cursor position is kept in `sessionStorage`, so after a navigation on the same site the cursor stays where it was.

**Mouse.** The real Playwright mouse (`page.mouse`) is moved along a slightly curved path with ease-in-out, about 60 steps per second, so the cursor never teleports. Clicks are real `mousedown`/`mouseup` events, so the browser does the default action (form submit, link navigation). Before each click the ripple is started from Python and played for 0.3 s: the search click starts a navigation, and a ripple started on `mousedown` would be gone before it is painted.

**Typing.** `page.keyboard.type` one character at a time, with a random 60–160 ms delay and a longer pause after spaces.

**DuckDuckGo specifics.**
- `form button[type="submit"]` matches two buttons. The first one is hidden (0 px wide); the visible one appears in the suggestion box after typing. The code always takes the first *visible* match.
- The suggestion box grows while suggestions arrive, and the results list shifts while it loads. Before moving, the code waits until the target stops moving, and after the move it checks that the cursor is still on it.
- The search is a full page navigation, so the old JS context is destroyed. Nothing is evaluated in the page at that moment: Python waits for the results with `wait_for_selector`, which survives the navigation, with a 30 s timeout (results are rendered client-side and can be slow).
- `ShowCallout` ignores a "context was destroyed" error: a callout is decoration and must not fail the run.

**Callout.** `window.ShowCallout(text, bgColor, textColor, fade)` in the page and `qscript.ShowCallout(page, text, bgColor, textColor, fade)` in Python. A yellow box with black 16pt text, sized to the text, 2 s fade-in and 2 s fade-out. The sample shows the company and the city from the CSV row.

**Video.** `browser.new_context(record_video_dir=PATH_TO_VIDEO_FOLDER, record_video_size=1280×720)`, viewport of the same size. After the scenario the context is closed (this writes the file), then the video is moved to `<PATH_TO_VIDEO_FOLDER>/<company_name>.webm` and the absolute path is returned. Characters that are not allowed in Windows file names are replaced with `_`. An existing file with the same name is replaced.

**CSV fields.** The sample uses `URL`, `City` and `Company Name`. `Language` becomes the browser locale (`de` gives the German DuckDuckGo interface; not a language code falls back to `en-US`, so the result does not depend on the recording machine). `Country`, `Postcode`, `Street` and `HouseNo` are accepted for the real Magento scenario and not used by the sample.

**Errors.** Each step has a name. If one fails, the function prints the step and the reason, closes the browser, keeps the partial video as `<company_name>.failed.webm` for debugging and returns `None`. `capture_video.py` goes on with the next row.

```
[2/2] Broken Co (Nowhere)
  failed at step 'open the page': TimeoutError: Page.wait_for_selector: Timeout 8000ms exceeded.
  partial video kept: ...\video\Broken Co.failed.webm
  FAILED
```

### Headless

`HEADLESS = False` in `qscript.py`, the browser runs with a window. I checked headless mode first: DuckDuckGo answers the search with its error page (`static-pages/home-error/418.html`), so the results never appear. With a window the same code works. I did not try to hide the automation from DuckDuckGo (user agent tricks and so on), it is not the point of the task. `--headless` stays for other sites.

### Tests

```bash
python -m unittest discover -s tests -v
```

They do not need the internet: a small local site in `tests/site/` copies the DuckDuckGo flow (a `textarea[name="q"]`, a form submit with a full navigation, results with `data-testid="result-title-a"` that appear after 1.5 s). The tests check:
- CSV reading: `;` inside quotes, `""` escapes, UTF-8 with or without BOM, empty lines, a missing column;
- in a real Chromium: arrow size and colours, the tip on the pointer, the highlight centred and behind the arrow, `pointer-events: none` (a click through the cursor still reaches the textarea), the ripple appears and disappears, the callout is 16pt and yellow, the cursor stays in place after a navigation;
- end to end: `capture_video.main()` on two rows; the good row gives `Good Co.webm` with a valid WebM header in a folder that did not exist, the server sees the typed query and the click on the first result; the broken row returns `None` and the exit code is 1.

### Checked by hand

On Windows 11 against the live DuckDuckGo on 3 November 2026: `input.csv` (Berlin, Stockholm, Manchester) gave 3 of 3 videos in several runs on Python 3.11, and once more from a fresh clone with a new Python 3.14 environment installed exactly as in "Run". I watched the frames of every video: the cursor, the highlight, the typing, the ripple on each click, the results and the opened first result. One thing I cannot control: the first result for Manchester was designrush.com, which shows its own "security verification" page, so that video ends on it.

### Not done

- No retries if DuckDuckGo is slow or changes its markup; the row fails with a clear step name instead.
- A first result that opens in a new tab is not handled (DuckDuckGo opens it in the same tab).
- The CI does not run the live DuckDuckGo scenario: CI has no screen, and DuckDuckGo blocks headless browsers. The live run was checked by hand, see above.

Made with an AI assistant (Claude); I went through the decisions and can explain them.
