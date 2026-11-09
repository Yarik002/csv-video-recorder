"""Video web service: upload a video, look it up by company name, play it.

Internship task 1: the database is PostgreSQL (it was SQLite in Test Task 2).
In Docker, Apache + mod_wsgi runs the app (Dockerfile, apache/video-service.conf,
web_service.wsgi). Without Docker it still runs on the Flask dev server:
    python web_service.py          # listens on 0.0.0.0:8000

Database settings come only from environment variables:
    DB_HOST, DB_PORT (default 5432), DB_NAME, DB_USER, DB_PASSWORD
    DB_WAIT_SECONDS  how long to wait for the database on start (default 60)

Endpoints:
    POST /upload                          multipart: file, company_name -> {company_name, video_url}
    GET  /get_video_url?company_name=...  -> {company_name, video_url}, 404 if unknown
    GET  /media/<filename>                the stored video file (Range requests work)
    GET  /videos                          all records

The uploads/ folder is next to this file. The database keeps only the path and
the URL, the video stays on disk.
"""

import datetime
import hashlib
import os
import re
import sys
import tempfile
import time
from urllib.parse import quote

import psycopg2
import psycopg2.extras
from flask import Flask, abort, current_app, g, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD_MB = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_name TEXT        NOT NULL UNIQUE,
    file_path    TEXT        NOT NULL,  -- absolute path to the file on disk
    video_url    TEXT        NOT NULL,  -- public URL to the video
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

# Keys for pg_advisory_xact_lock: a lock that lives until the end of the transaction
# and is shared by all processes that use this database.
SCHEMA_LOCK = 7270001   # creating the table (two Apache processes start at once)
UPLOADS_LOCK = 7270002  # choosing a file name + moving the file + writing the row

# Environment variable for every psycopg2.connect() argument.
DB_ENV = {"host": "DB_HOST", "port": "DB_PORT", "dbname": "DB_NAME", "user": "DB_USER", "password": "DB_PASSWORD"}

# Extensions a browser can play, with the Content-Type to send. Set by hand:
# on Windows the mimetypes module reads the registry and can be wrong.
VIDEO_TYPES = {
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".ogv": "video/ogg",
    ".ogg": "video/ogg",
}
DEFAULT_EXT = ".webm"
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}


class FileBusyError(Exception):
    """The target file is open (for example a player streams it) and Windows cannot replace it."""


def log(message):
    # Under mod_wsgi stderr goes to the Apache error log (docker compose logs web).
    print(f"[web_service] {message}", file=sys.stderr, flush=True)


# ---------- database layer ----------

def db_settings(environ=None):
    """psycopg2.connect() arguments from DB_* environment variables. Nothing is hard-coded."""
    environ = os.environ if environ is None else environ
    settings = {key: environ.get(name, "") for key, name in DB_ENV.items()}
    settings["port"] = settings["port"] or "5432"
    missing = [DB_ENV[key] for key, value in settings.items() if not value]
    if missing:
        raise RuntimeError("database settings missing in the environment: " + ", ".join(missing))
    return settings


def describe(settings):
    """user@host:port/dbname, without the password, for logs and errors."""
    return f"{settings['user']}@{settings['host']}:{settings['port']}/{settings['dbname']}"


def connect(settings):
    # RealDictCursor: rows come back as dicts, like sqlite3.Row in Test Task 2.
    return psycopg2.connect(connect_timeout=5, cursor_factory=psycopg2.extras.RealDictCursor, **settings)


def wait_for_db(settings, timeout):
    """Connects, retrying once a second until the database accepts connections.
    Docker Compose already starts web after db is healthy; this covers the rest
    (a restart of db, running without Compose)."""
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            conn = connect(settings)
        except psycopg2.OperationalError as exc:
            reason = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"database {describe(settings)} is not ready after {timeout:g} s: {reason}") from exc
            log(f"waiting for the database {describe(settings)} (attempt {attempt}): {reason}")
            time.sleep(1)
        else:
            if attempt > 1:
                log(f"database is ready after {attempt} attempts")
            return conn


def lock(conn, key):
    """Takes an advisory lock till the end of the current transaction (other processes wait)."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


def init_db(conn):
    """Creates the table if it is not there yet. Under a lock: CREATE TABLE IF NOT EXISTS
    from two processes at the same moment can still fail in PostgreSQL."""
    with conn:  # one transaction, committed at the end
        lock(conn, SCHEMA_LOCK)
        with conn.cursor() as cur:
            cur.execute(SCHEMA)


def get_db():
    """One connection per request, kept in flask.g. A psycopg2 connection is not shared
    between threads, and Apache runs several threads and processes."""
    if "db" not in g:
        g.db = connect(current_app.config["DB"])
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()  # an unfinished transaction is rolled back


def fetch(conn, query, args=(), one=False):
    with conn.cursor() as cur:
        cur.execute(query, args)
        return cur.fetchone() if one else cur.fetchall()


def find_video(conn, company_name):
    return fetch(conn, "SELECT * FROM videos WHERE company_name = %s", (company_name,), one=True)


def list_videos(conn):
    return fetch(conn, "SELECT * FROM videos ORDER BY company_name")


def upsert_video(conn, company_name, file_path, video_url):
    """Insert, or update the row of the same company: id and created_at stay."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO videos (company_name, file_path, video_url) VALUES (%s, %s, %s)
            ON CONFLICT (company_name) DO UPDATE SET
                file_path  = EXCLUDED.file_path,
                video_url  = EXCLUDED.video_url,
                updated_at = NOW()
            """,
            (company_name, file_path, video_url),
        )


def path_owner(conn, file_path):
    """Company that already uses this file, or None. Compared without case: on Windows
    and macOS (python web_service.py there) 'acme.webm' and 'ACME.webm' are the same file."""
    row = fetch(conn, "SELECT company_name FROM videos WHERE lower(file_path) = lower(%s)",
                (file_path,), one=True)
    return row["company_name"] if row else None


def to_json(row):
    """TIMESTAMPTZ comes back as datetime; JSON gets ISO 8601 text."""
    return {key: value.isoformat() if isinstance(value, datetime.datetime) else value
            for key, value in row.items()}


# ---------- file names ----------

def _short_hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


def safe_name(company_name):
    """'ACME GmbH' -> 'ACME_GmbH'. Only [A-Za-z0-9._-] are kept (no werkzeug import)."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", company_name)
    name = re.sub(r"_{2,}", "_", name).strip("._-")[:80]
    if not re.search(r"[A-Za-z0-9]", name) or name.split(".")[0].upper() in WINDOWS_RESERVED:
        # e.g. a name in Cyrillic: nothing readable is left, the hash keeps it unique.
        name = f"video_{_short_hash(company_name)}"
    return name


def choose_file_name(conn, uploads_dir, company_name, ext):
    """File name for this company. Two companies never share a file: 'A B' and
    'A_B' (or 'acme' and 'ACME') would get the same name, so the second one gets
    a hash suffix."""
    base = safe_name(company_name)
    for candidate in (base, f"{base}_{_short_hash(company_name)}"):
        path = os.path.join(uploads_dir, candidate + ext)
        if path_owner(conn, path) in (None, company_name):
            return candidate + ext
    raise RuntimeError(f"no free file name for {company_name!r}")  # needs a sha1 collision


def replace_file(src, dst, attempts=5):
    """os.replace is atomic: a player never sees half a file. On Windows it fails
    while the old file is open (for example a browser is streaming it), so retry."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise FileBusyError(os.path.basename(dst))
            time.sleep(0.2)


def _remove_quietly(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        current_app.logger.warning("could not remove %s: %s", path, exc)


def remove_if_unused(conn, path):
    """Deletes a file no row points to any more (e.g. the company sent .mp4 after .webm).
    Under the uploads lock, so no other upload can take this name at the same moment."""
    with conn:
        lock(conn, UPLOADS_LOCK)
        if path_owner(conn, path) is None:
            _remove_quietly(path)


# ---------- web service ----------

def error(status, message, **extra):
    return jsonify(error=message, **extra), status


def create_app(data_dir=None, db=None):
    """data_dir holds uploads/ (default: the folder of this file).
    db: psycopg2.connect() arguments (default: from the DB_* environment variables).
    Waits for the database and creates the table before it returns."""
    data_dir = os.path.abspath(data_dir or HERE)
    settings = dict(db) if db is not None else db_settings()
    app = Flask(__name__)
    app.config.update(
        DB=settings,
        UPLOAD_FOLDER=os.path.join(data_dir, "uploads"),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    )
    app.json.ensure_ascii = False  # company names in Cyrillic stay readable

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    for name in os.listdir(app.config["UPLOAD_FOLDER"]):
        if name.startswith(".upload-") and name.endswith(".part"):  # left by a crash
            os.remove(os.path.join(app.config["UPLOAD_FOLDER"], name))

    conn = wait_for_db(settings, float(os.environ.get("DB_WAIT_SECONDS", "60")))
    try:
        init_db(conn)
    finally:
        conn.close()
    app.teardown_appcontext(close_db)

    @app.post("/upload")
    def upload():
        company_name = (request.form.get("company_name") or "").strip()
        file = request.files.get("file")
        if not company_name:
            return error(400, "company_name is required")
        if file is None:
            return error(400, "file is required (multipart field 'file')")
        ext = os.path.splitext(file.filename or "")[1].lower() or DEFAULT_EXT
        if ext not in VIDEO_TYPES:
            return error(400, f"unsupported file type {ext!r}, allowed: {', '.join(VIDEO_TYPES)}")

        uploads = current_app.config["UPLOAD_FOLDER"]
        # Receive into a temporary file first, outside the database lock.
        fd, tmp_path = tempfile.mkstemp(dir=uploads, prefix=".upload-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as out:
                file.save(out)
            if os.path.getsize(tmp_path) == 0:
                return error(400, "file is empty")

            conn = get_db()
            # One transaction under the uploads lock: choosing the file name, moving the
            # file and writing the row happen as one step for one upload, even when
            # several Apache processes handle uploads at the same moment.
            with conn:  # commit at the end, rollback on any error
                lock(conn, UPLOADS_LOCK)
                old = find_video(conn, company_name)
                filename = choose_file_name(conn, uploads, company_name, ext)
                file_path = os.path.join(uploads, filename)
                replace_file(tmp_path, file_path)
                video_url = request.host_url + "media/" + quote(filename)
                upsert_video(conn, company_name, file_path, video_url)
            if old is not None and old["file_path"] != file_path:
                remove_if_unused(conn, old["file_path"])
        except FileBusyError as exc:
            return error(503, f"{exc} is being read right now (Windows cannot replace an open file), try again")
        finally:
            _remove_quietly(tmp_path)  # already moved on success

        return jsonify(company_name=company_name, video_url=video_url)

    @app.get("/get_video_url")
    def get_video_url():
        company_name = (request.args.get("company_name") or "").strip()
        if not company_name:
            return error(400, "company_name is required")
        row = find_video(get_db(), company_name)
        if row is None:
            return error(404, "company not found", company_name=company_name)
        return jsonify(company_name=row["company_name"], video_url=row["video_url"])

    @app.get("/media/<filename>")  # no "/" in filename: uploads/ has no subfolders
    def media(filename):
        # In Docker Apache serves /media/ itself (Alias), this route is for python web_service.py.
        if filename.startswith("."):  # unfinished .part uploads
            abort(404)
        mimetype = VIDEO_TYPES.get(os.path.splitext(filename)[1].lower())
        # send_from_directory refuses paths outside uploads/ and answers Range requests (206),
        # so the player can seek.
        return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename, mimetype=mimetype)

    @app.get("/videos")
    def videos():
        return jsonify(videos=[to_json(row) for row in list_videos(get_db())])

    # Errors are JSON too, so the client can show the reason.
    for code in (400, 404, 405, 413, 500):
        app.register_error_handler(code, _json_error)
    for exc_class in (psycopg2.OperationalError, psycopg2.InterfaceError):
        app.register_error_handler(exc_class, _db_down)

    return app


def _json_error(exc):
    code = getattr(exc, "code", 500) or 500
    if code == 413:
        limit_mb = current_app.config["MAX_CONTENT_LENGTH"] / (1024 * 1024)
        return error(413, f"file is too large (limit {limit_mb:g} MB)")
    message = getattr(exc, "description", None) if code != 500 else "internal server error"
    body, status = error(code, message or "error")
    if code == 405:
        body.headers["Allow"] = ", ".join(exc.valid_methods or [])
    return body, status


def _db_down(exc):
    """The database went away after start (restart of the db container, network)."""
    log(f"database error: {exc.__class__.__name__}: {str(exc).strip()}")
    return error(503, "database is not available, try again")


if __name__ == "__main__":
    application = create_app()
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8000"))
    print(f"Database: {describe(application.config['DB'])}")
    print(f"Uploads:  {application.config['UPLOAD_FOLDER']}")
    # Development server, one thread per request. debug stays off.
    application.run(host=host, port=port, threaded=True)
