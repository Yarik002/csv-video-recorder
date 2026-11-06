"""Web service for Test Task 2: upload a video, look it up by company name, play it.

Run:
    python web_service.py          # listens on 0.0.0.0:8000
    FLASK_HOST=127.0.0.1 FLASK_PORT=8080 python web_service.py

Endpoints:
    POST /upload                          multipart: file, company_name -> {company_name, video_url}
    GET  /get_video_url?company_name=...  -> {company_name, video_url}, 404 if unknown
    GET  /media/<filename>                the stored video file (Range requests work)
    GET  /videos                          all records

The database (videos.db) and the uploads/ folder are created next to this file
on start. The database keeps only the path and the URL, the video stays on disk.
"""

import hashlib
import os
import re
import sqlite3
import tempfile
import time
from urllib.parse import quote

from flask import Flask, abort, current_app, g, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
MAX_UPLOAD_MB = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT    NOT NULL UNIQUE,
    file_path    TEXT    NOT NULL,  -- absolute path to the file on disk
    video_url    TEXT    NOT NULL,  -- public URL to the video
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

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
    """The target file is open in another program (Windows only) and cannot be replaced."""


# ---------- database layer ----------

def connect(db_path):
    # timeout + busy_timeout: a writer waits for the lock instead of failing
    # with "database is locked".
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path):
    """Creates videos.db and the table. WAL mode is stored in the file itself."""
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # readers do not block the writer
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_db():
    """One connection per request, kept in flask.g (Flask serves requests in threads)."""
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def find_video(conn, company_name):
    return conn.execute(
        "SELECT * FROM videos WHERE company_name = ?", (company_name,)
    ).fetchone()


def list_videos(conn):
    return conn.execute("SELECT * FROM videos ORDER BY company_name").fetchall()


def upsert_video(conn, company_name, file_path, video_url):
    """Insert, or update the row of the same company: id and created_at stay."""
    conn.execute(
        """
        INSERT INTO videos (company_name, file_path, video_url) VALUES (?, ?, ?)
        ON CONFLICT(company_name) DO UPDATE SET
            file_path  = excluded.file_path,
            video_url  = excluded.video_url,
            updated_at = datetime('now')
        """,
        (company_name, file_path, video_url),
    )


def path_owner(conn, file_path):
    """Company that already uses this file, or None. NOCASE: on Windows and macOS
    'acme.webm' and 'ACME.webm' are the same file."""
    row = conn.execute(
        "SELECT company_name FROM videos WHERE file_path = ? COLLATE NOCASE", (file_path,)
    ).fetchone()
    return row["company_name"] if row else None


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
    except OSError:
        pass


# ---------- web service ----------

def error(status, message, **extra):
    return jsonify(error=message, **extra), status


def create_app(data_dir=None):
    """data_dir holds videos.db and uploads/ (default: the folder of this file)."""
    data_dir = os.path.abspath(data_dir or HERE)
    app = Flask(__name__)
    app.config.update(
        DATABASE=os.path.join(data_dir, "videos.db"),
        UPLOAD_FOLDER=os.path.join(data_dir, "uploads"),
        MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    )
    app.json.ensure_ascii = False  # company names in Cyrillic stay readable

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    init_db(app.config["DATABASE"])
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
            # BEGIN IMMEDIATE takes the write lock now, so choosing the file name,
            # moving the file and writing the row happen as one step for one upload.
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = find_video(conn, company_name)
                filename = choose_file_name(conn, uploads, company_name, ext)
                file_path = os.path.join(uploads, filename)
                replace_file(tmp_path, file_path)
                video_url = request.host_url + "media/" + quote(filename)
                upsert_video(conn, company_name, file_path, video_url)
                if old is not None and old["file_path"] != file_path:
                    # e.g. the company sent .mp4 instead of .webm. Removed while we still
                    # hold the lock, so no other upload can take this name in between.
                    _remove_quietly(old["file_path"])
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        except FileBusyError as exc:
            return error(503, f"{exc} is open in another program, try again")
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

    @app.get("/media/<path:filename>")
    def media(filename):
        if filename.startswith("."):  # unfinished .part uploads
            abort(404)
        mimetype = VIDEO_TYPES.get(os.path.splitext(filename)[1].lower())
        # send_from_directory refuses paths outside uploads/ and answers Range requests (206),
        # so the player can seek.
        return send_from_directory(current_app.config["UPLOAD_FOLDER"], filename, mimetype=mimetype)

    @app.get("/videos")
    def videos():
        return jsonify(videos=[dict(row) for row in list_videos(get_db())])

    # Errors are JSON too, so the client can show the reason.
    for code in (400, 404, 405, 413, 500):
        app.register_error_handler(code, _json_error)

    return app


def _json_error(exc):
    code = getattr(exc, "code", 500) or 500
    if code == 413:
        limit_mb = current_app.config["MAX_CONTENT_LENGTH"] / (1024 * 1024)
        return error(413, f"file is too large (limit {limit_mb:g} MB)")
    message = getattr(exc, "description", None) if code != 500 else "internal server error"
    return error(code, message or "error")


if __name__ == "__main__":
    application = create_app()
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", "8000"))
    print(f"Database: {application.config['DATABASE']}")
    print(f"Uploads:  {application.config['UPLOAD_FOLDER']}")
    # Development server, one thread per request. debug stays off.
    application.run(host=host, port=port, threaded=True)
