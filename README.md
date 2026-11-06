# CSV video recorder + video web service

[![CI](https://github.com/Yarik002/csv-video-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/Yarik002/csv-video-recorder/actions/workflows/ci.yml)

Two test tasks in one repository:

- **Task 2** (latest, start here): a Flask web service with SQLite. After recording, every video is uploaded, stored, can be looked up by company name and opens in Chromium. How to run it is right below, details in [Task 2: details](#task-2-details).
- **Task 1**: the CSV-driven browser recorder. It is unchanged; it is described in [Task 1: CSV-driven recorder](#task-1-csv-driven-recorder). The exact version I sent for Task 1 is the tag [`task-1`](https://github.com/Yarik002/csv-video-recorder/tree/task-1).

## Task 2: how to run

Python 3.9 or newer. Install once.

Windows (cmd or PowerShell):

```bat
git clone https://github.com/Yarik002/csv-video-recorder.git
cd csv-video-recorder
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

If PowerShell says that running scripts is disabled, run `Set-ExecutionPolicy -Scope Process Bypass` first, or use cmd.

Linux or macOS:

```bash
git clone https://github.com/Yarik002/csv-video-recorder.git
cd csv-video-recorder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

Then two terminals, both in the `csv-video-recorder` folder with the venv activated (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` on Linux/macOS).

Terminal 1, the web service:

```bash
python web_service.py
```

```
Database: C:\...\csv-video-recorder\videos.db
Uploads:  C:\...\csv-video-recorder\uploads
 * Running on all addresses (0.0.0.0)
 * Running on http://127.0.0.1:8000
```

Terminal 2, record the 3 rows of `input.csv`, upload each video and open it in Chromium:

```bash
python capture_video.py --server http://127.0.0.1:8000
```

```
[1/3] Acme Retail GmbH (Berlin)
  saved: C:\...\csv-video-recorder\video\Acme Retail GmbH.webm (34 s)
  uploaded: http://127.0.0.1:8000/media/Acme_Retail_GmbH.webm
[2/3] Nordic Home; Garden AB (Stockholm)
  saved: C:\...\csv-video-recorder\video\Nordic Home; Garden AB.webm (34 s)
  uploaded: http://127.0.0.1:8000/media/Nordic_Home_Garden_AB.webm
[3/3] Atlantic Supplies Ltd (Manchester)
  saved: C:\...\csv-video-recorder\video\Atlantic Supplies Ltd.webm (33 s)
  uploaded: http://127.0.0.1:8000/media/Atlantic_Supplies_Ltd.webm
Done: 3 of 3 videos saved, 3 uploaded, 3 previewed.
```

The recorder needs a visible browser window: DuckDuckGo answers headless browsers with an error page (see [Headless](#headless)).

After each upload a Chromium window opens the returned URL and its built-in player plays the video for 10 seconds (`--view-seconds N`; `--view-seconds 0` keeps it open until you press Enter):

![The preview window: Chromium plays the uploaded video from the service](docs/img/preview.png)

Notes:
- The service listens on `0.0.0.0:8000`. On the first start Windows may ask whether Python may use the network: allow it, or keep the service local with `FLASK_HOST=127.0.0.1`.
- `FLASK_HOST` and `FLASK_PORT` change the address: `set FLASK_PORT=8080` (cmd), `$env:FLASK_PORT="8080"` (PowerShell), `FLASK_PORT=8080 python web_service.py` (Linux/macOS).

Ask the service yourself (in PowerShell write `curl.exe`, plain `curl` there is another command):

```bash
curl "http://127.0.0.1:8000/get_video_url?company_name=Acme%20Retail%20GmbH"
curl -X POST http://127.0.0.1:8000/upload -F "company_name=ACME GmbH" -F "file=@docs/sample.webm"
curl http://127.0.0.1:8000/videos
```

Answers:

```
{"company_name":"Acme Retail GmbH","video_url":"http://127.0.0.1:8000/media/Acme_Retail_GmbH.webm"}
{"company_name":"ACME GmbH","video_url":"http://127.0.0.1:8000/media/ACME_GmbH.webm"}
{"videos":[{"company_name":"ACME GmbH","created_at":"2026-11-06 11:10:50","file_path":"C:\\...\\uploads\\ACME_GmbH.webm","id":10,"updated_at":"2026-11-06 11:10:50","video_url":"http://127.0.0.1:8000/media/ACME_GmbH.webm"}, ...]}
```

Tests, offline, about 2 minutes, they open Chromium windows (on Linux without a screen run them with `xvfb-run`):

```bash
python -m unittest discover -s tests -v
```

### Acceptance checklist

Every item was checked by hand on Windows 11 on 6 November 2026 against the live DuckDuckGo, and most of them also by an automated test that runs in CI on Python 3.9, 3.12 and 3.14.

| # | Item | Automated test | By hand |
|---|---|---|---|
| 1 | `pip install -r requirements.txt` works on a clean machine | CI installs into a fresh Ubuntu on 3.9, 3.12, 3.14 | fresh clone from GitHub into an empty folder, new venv on Python 3.14 and on 3.11 |
| 2 | `python web_service.py` starts and creates `videos.db` and `uploads/` | `RunAsScriptTest` (real `python web_service.py`), `StartupTest` (exact schema, WAL) | yes, both appeared on start |
| 3 | `POST /upload` returns 200 and a working URL | `UploadTest`, `ClientTest` | 3 recorded videos + curl |
| 4 | The URL serves the file (HTTP 200) and plays in a browser | `MediaTest` (200, `video/webm`, Range gives 206), `test_preview_opens_the_url` | Chromium played it, screenshot above (0:05 of 0:31, not paused) |
| 5 | `GET /get_video_url` returns the URL; 404 for unknown names | `LookupAndListTest`, `test_unknown_company` | 3 companies 200, made-up name 404 |
| 6 | The DB stores `company_name`, `file_path`, `video_url` | `test_upload_returns_url_and_saves_file` (absolute path, no BLOB column) | printed the table |
| 7 | Re-upload of the same company updates the record, no duplicates | `test_reupload_updates_same_row`, 20 parallel uploads | second batch run: still 3 rows, same `id` and `created_at`, new `updated_at` |
| 8 | `capture_video.py --server http://127.0.0.1:8000` uploads each video and opens it in Chromium | `EndToEndTest` (local copy of the search page), `BatchLoopTest` | 3 of 3 uploaded and previewed |
| 9 | Works end to end on a clean machine after `pip install` and `playwright install chromium` | CI | fresh clone, Python 3.14, steps above: 50 tests pass, live run 3 of 3 uploaded and previewed |

Also checked by hand: with the service stopped, the batch still records all 3 videos, logs `upload failed: cannot reach http://127.0.0.1:8000/upload: ConnectionError` for each and exits with code 1; `--view-seconds 0` keeps the window until Enter.

## Task 2: details

### Files

| File | What it does |
|---|---|
| `web_service.py` | New. The Flask service and the database layer (`create_app`, `init_db`, `get_db`, `upsert_video`, `find_video`, `list_videos`) |
| `video_client.py` | New. `UploadVideo(server_url, video_path, company_name) -> str` and `GetVideoURL(server_url, company_name) -> str` with the exact signatures, plus `PreviewVideo(video_url, view_seconds)` |
| `capture_video.py` | Changed. `--server URL` and `--view-seconds N`. Without `--server` it works and prints exactly as in Task 1 |
| `requirements.txt` | Changed. `playwright==1.60.0`, `flask==3.1.3`, `requests==2.32.5` |
| `qscript.py` | Not changed (Task 1) |
| `tests/test_web_service.py`, `tests/test_upload_preview.py` | New tests for Task 2 |

### Endpoints

| Method and path | Answer |
|---|---|
| `POST /upload` (multipart: `file`, `company_name`) | 200 `{"company_name": "...", "video_url": "http://<host>:<port>/media/<file>"}` |
| `GET /get_video_url?company_name=<name>` | 200 the same JSON; 404 `{"error": "company not found", ...}` |
| `GET /media/<filename>` | the video file, `Content-Type: video/webm`, Range requests answered with 206, so the player can seek |
| `GET /videos` | `{"videos": [{id, company_name, file_path, video_url, created_at, updated_at}, ...]}`, sorted by name |

Every error is JSON with an `error` field: 400 for a missing or empty `file`, a missing or blank `company_name`, an extension that is not `.webm`, `.mp4`, `.ogv` or `.ogg` (a file without an extension is taken as `.webm`); 404; 405 for a wrong method; 413 for a file over 500 MB; 503 if the file is being read at that moment (see below). Spaces around `company_name` are removed.

### Database

`videos.db` next to `web_service.py`, created on start with exactly the table from the task:

```sql
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT    NOT NULL UNIQUE,
    file_path    TEXT    NOT NULL,  -- absolute path to the file on disk
    video_url    TEXT    NOT NULL,  -- public URL to the video
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

Only the path and the URL are stored, the video itself is in `uploads/`. `datetime('now')` in SQLite is UTC. Re-upload is one statement:

```sql
INSERT INTO videos (company_name, file_path, video_url) VALUES (?, ?, ?)
ON CONFLICT(company_name) DO UPDATE SET
    file_path = excluded.file_path, video_url = excluded.video_url, updated_at = datetime('now')
```

so the row keeps its `id` and `created_at`.

### How the hints from the task are done

- **Connection per request.** `get_db()` opens a connection and keeps it in `flask.g`; `teardown_appcontext` closes it. No connection is shared between threads.
- **"database is locked".** `sqlite3.connect(timeout=10)` plus `PRAGMA busy_timeout = 10000`: a second writer waits instead of failing. `PRAGMA journal_mode = WAL` is set once in `init_db` (it is stored in the file): readers do not block the writer. A test sends 20 uploads at the same moment to a real threaded server: all 200, no "locked".
- **URL from `request.host_url`.** `video_url = request.host_url + "media/" + <file name>`. A request to `http://127.0.0.1:8000` gives `http://127.0.0.1:8000/media/...`, a request from another computer to `http://192.168.1.5:8000` gives that address.
- **`send_from_directory`** serves `uploads/`. It refuses paths outside the folder (`/media/../videos.db` is 404, tested) and answers Range requests.
- **Path and URL in the DB, not bytes.** Tested: no BLOB column, the file on disk is byte-identical to the upload.
- **Multipart from the client.** `requests` with `files={"file": (filename, fileobj, "video/webm")}, data={"company_name": ...}`.
- **Direct `.webm` URL in Chromium** shows its built-in player; `PreviewVideo` launches Chromium with `headless=False` and goes to the URL.
- **Lazy import.** `capture_video.py` imports `video_client` only when `--server` is given, and `video_client` imports `requests` and Playwright inside the functions. A test runs the recorder with `requests` blocked: without `--server` it works, with `--server` it says `--server needs the requests package`.

### Decisions worth knowing

- **File names.** `ACME GmbH` becomes `ACME_GmbH.webm`, as in the task: every character except `A-Z a-z 0-9 . _ -` becomes `_`. This is my own small rule, not `werkzeug.utils.secure_filename`: werkzeug comes only as a dependency of Flask, and the task lists flask, requests and playwright as the allowed libraries. Different companies can give the same name (`A B` and `A_B`; `acme` and `ACME` on Windows, where file names ignore case), so before saving the service checks in the database whether another company already uses this file; if so, it adds the first 8 characters of the SHA-1 of the company name (`A_B_2429e5cb.webm`). Names with no Latin letters or digits (`ООО Ромашка`) and Windows device names (`CON`) get `video_<hash>`.
- **Atomic save.** The upload is written to a temporary `.part` file in `uploads/`, then moved over the old file with `os.replace`, so a player never gets half a file. Choosing the name, moving the file and writing the row run under one write lock (`BEGIN IMMEDIATE`), so two parallel uploads cannot pick the same name.
- **A file that is being played.** On Windows a file that is open cannot be replaced. I checked it: while a client was downloading `Enter_Check_Co.webm`, a re-upload of that company got `503 {"error": "... is being read right now ..."}` after 5 tries in 0.8 s; after the download was closed, the same upload returned 200.
- **Errors in the batch.** A failed upload prints `upload failed: <reason>`, a failed preview prints `preview failed: <reason>`, and the loop goes on with the next row. The last line counts saved, uploaded and previewed videos; the exit code is 1 if any of them failed.
- **`--view-seconds`** default is 10. A negative value or `--view-seconds` without `--server` is an argument error. If you close the preview window yourself before the time is up, the batch just goes on.
- **Development server.** `app.run(threaded=True)`, debug off, as the task asks.

### Limits

- The URL is saved at upload time, as the schema says. If the service is later opened under another address, old rows keep the old host until the video is uploaded again.
- `0.0.0.0` is the address the service listens on, not an address to open: use `127.0.0.1` on the same computer or the computer's IP from another one.
- The host in `video_url` comes from the request, as the task says, so a client decides which host is saved. `/videos` shows absolute paths on the server. Fine for a local tool, not for the open internet.
- `ON CONFLICT ... DO UPDATE` needs SQLite 3.24 (2018) or newer. Python on Windows, macOS and current Linux has it (3.45 here, also in CI).
- There is no login, no delete endpoint and no HTML page: the task does not ask for them. For real use I would put it behind gunicorn and nginx and keep the files in object storage.

### Tests for Task 2

`tests/test_web_service.py` (27 tests, about 6 s): `python web_service.py` with `FLASK_HOST`/`FLASK_PORT` keeps `videos.db` and `uploads/` next to the script, not in the current folder; schema and WAL; upload JSON, file and row; re-upload keeps one row, `id` and `created_at`; a new extension removes the old file; name collisions (`A B`/`A_B`, `acme`/`ACME`), Cyrillic and `CON`; every 400/404/405/413/503 is JSON and stores nothing; `/media` 200, Range 206, path traversal 404; 20 parallel uploads on a real threaded server, also with colliding names.

`tests/test_upload_preview.py` (13 tests): exact client signatures; `UploadVideo` and `GetVideoURL` against a running service; `VideoNotFoundError` for an unknown name; a clear error when the service is down; the preview window really requests the video; `--view-seconds 0` waits for Enter; a failed upload or preview does not stop the loop; lazy import; end to end: record on the local copy of the search page, upload, preview.

The preview needs a screen, so CI runs the tests with `xvfb-run`.

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
| `requirements.txt` | `playwright` (Task 2 added `flask` and `requests`) |
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
