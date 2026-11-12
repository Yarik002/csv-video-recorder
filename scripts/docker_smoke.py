"""Checks the running Docker setup from outside, with the client from Test Task 2.

    docker compose up --build -d --wait
    python scripts/docker_smoke.py upload   # upload docs/sample.webm (+ docs/sample.zoom.json), check the
                                            # URL, the lookup, and wait for the zoomed video (Internship task 2)
    docker compose down
    docker compose up -d --wait
    python scripts/docker_smoke.py check    # the record and the video are still there

SERVICE_URL changes the address (default http://localhost:8000). Needs only requests.
Exit code 0 if every check passed, 1 otherwise. CI runs exactly this (.github/workflows/ci.yml).
"""

import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_client  # noqa: E402

SERVER = os.environ.get("SERVICE_URL", "http://localhost:8000")
COMPANY = "Docker Smoke GmbH"
SAMPLE = os.path.join(ROOT, "docs", "sample.webm")
EXPECTED_URL = SERVER.rstrip("/") + "/media/Docker_Smoke_GmbH.webm"

failures = []
WEBM_MAGIC = bytes([0x1A, 0x45, 0xDF, 0xA3])  # EBML header of every .webm


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def check_video(url):
    with open(SAMPLE, "rb") as f:
        sample = f.read()
    resp = requests.get(url, timeout=30)
    check("GET video_url is 200", resp.status_code == 200, str(resp.status_code))
    check("Content-Type is video/webm", resp.headers.get("Content-Type", "").startswith("video/webm"),
          resp.headers.get("Content-Type", ""))
    check("the video is byte-identical to docs/sample.webm", resp.content == sample,
          f"{len(resp.content)} bytes, expected {len(sample)}")
    part = requests.get(url, headers={"Range": "bytes=0-99"}, timeout=30)
    check("Range request gives 206 and 100 bytes", part.status_code == 206 and len(part.content) == 100,
          f"{part.status_code}, {len(part.content)} bytes")


def video_row():
    videos = requests.get(SERVER.rstrip("/") + "/videos", timeout=30).json()["videos"]
    return next((v for v in videos if v["company_name"] == COMPANY), None)


def check_zoom(wait):
    """Internship task 2: UploadVideo sent docs/sample.zoom.json with the video, the worker
    container renders the zoom with ffmpeg and stores the URL in PostgreSQL."""
    deadline = time.monotonic() + wait
    row = video_row()
    while not (row and row.get("processed_video_url")) and time.monotonic() < deadline:
        time.sleep(3)
        row = video_row()
    url = (row or {}).get("processed_video_url") or ""
    check("the worker rendered the zoom, processed_video_url is in PostgreSQL",
          url.startswith(SERVER.rstrip("/") + "/media/zoomed/Docker_Smoke_GmbH-job"), str(row))
    if not url:
        return
    resp = requests.get(url, timeout=30)
    check("GET processed_video_url is 200 video/webm",
          resp.status_code == 200 and resp.headers.get("Content-Type", "").startswith("video/webm"),
          f"{resp.status_code} {resp.headers.get('Content-Type')}")
    check("the zoomed video is a WebM file", resp.content[:4] == WEBM_MAGIC and len(resp.content) > 100_000,
          f"{len(resp.content)} bytes")
    part = requests.get(url, headers={"Range": "bytes=0-99"}, timeout=30)
    check("Range request on the zoomed video gives 206", part.status_code == 206, str(part.status_code))


def check_lookup():
    url = video_client.GetVideoURL(SERVER, COMPANY)
    check("GET /get_video_url returns the URL", url == EXPECTED_URL, url)
    return url


def main(argv):
    if len(argv) != 2 or argv[1] not in ("upload", "check"):
        print(__doc__)
        return 2
    print(f"service: {SERVER}")
    try:
        if argv[1] == "upload":
            url = video_client.UploadVideo(SERVER, SAMPLE, COMPANY)
            check("POST /upload returns the URL", url == EXPECTED_URL, url)
        url = check_lookup()
        check_video(url)
        videos = requests.get(SERVER.rstrip("/") + "/videos", timeout=30).json()["videos"]
        row = next((v for v in videos if v["company_name"] == COMPANY), None)
        check("GET /videos has the record with the path in the uploads volume",
              row is not None and row["file_path"] == "/app/uploads/Docker_Smoke_GmbH.webm", str(row))
        check_zoom(float(os.environ.get("ZOOM_WAIT_SECONDS", "300")) if argv[1] == "upload" else 0)
    except (video_client.VideoServiceError, requests.RequestException, ValueError, KeyError) as exc:
        check("service answers", False, f"{exc.__class__.__name__}: {exc}")
    print("all checks passed" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
