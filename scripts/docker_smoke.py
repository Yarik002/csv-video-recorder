"""Checks the running Docker setup from outside, with the client from Test Task 2.

    docker compose up --build -d --wait
    python scripts/docker_smoke.py upload   # upload docs/sample.webm, check the URL and the lookup
    docker compose down
    docker compose up -d --wait
    python scripts/docker_smoke.py check    # the record and the video are still there

SERVICE_URL changes the address (default http://localhost:8000). Needs only requests.
Exit code 0 if every check passed, 1 otherwise. CI runs exactly this (.github/workflows/ci.yml).
"""

import os
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import video_client  # noqa: E402

SERVER = os.environ.get("SERVICE_URL", "http://localhost:8000")
COMPANY = "Docker Smoke GmbH"
SAMPLE = os.path.join(ROOT, "docs", "sample.webm")
EXPECTED_URL = SERVER.rstrip("/") + "/media/Docker_Smoke_GmbH.webm"

failures = []


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
    except (video_client.VideoServiceError, requests.RequestException, ValueError, KeyError) as exc:
        check("service answers", False, f"{exc.__class__.__name__}: {exc}")
    print("all checks passed" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
