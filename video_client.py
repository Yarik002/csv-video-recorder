"""Client functions for web_service.py, used by capture_video.py --server.

requests and playwright are imported inside the functions: importing this
module needs nothing, and the recorder works without requests when --server
is not used.
"""

import os

MIME_TYPES = {".webm": "video/webm", ".mp4": "video/mp4", ".ogv": "video/ogg", ".ogg": "video/ogg"}


class VideoServiceError(RuntimeError):
    """The server answered with an error or could not be reached."""


class VideoNotFoundError(VideoServiceError, LookupError):
    """The server has no video for this company (HTTP 404)."""


def _error_text(resp):
    try:
        return resp.json().get("error") or resp.text
    except ValueError:  # not JSON
        return resp.text[:200]


def _call(method, url, **kwargs):
    import requests

    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise VideoServiceError(f"cannot reach {url}: {exc.__class__.__name__}") from exc


def UploadVideo(server_url: str, video_path: str, company_name: str) -> str:
    """Uploads a video file to the server and returns its URL."""
    filename = os.path.basename(video_path)
    mime = MIME_TYPES.get(os.path.splitext(filename)[1].lower(), "application/octet-stream")
    with open(video_path, "rb") as f:
        resp = _call(
            "POST",
            server_url.rstrip("/") + "/upload",
            files={"file": (filename, f, mime)},
            data={"company_name": company_name},
            timeout=(5, 300),  # connect, read
        )
    if resp.status_code != 200:
        raise VideoServiceError(f"HTTP {resp.status_code}: {_error_text(resp)}")
    return resp.json()["video_url"]


def GetVideoURL(server_url: str, company_name: str) -> str:
    """Returns the video URL for the given company name."""
    resp = _call(
        "GET",
        server_url.rstrip("/") + "/get_video_url",
        params={"company_name": company_name},
        timeout=(5, 30),
    )
    if resp.status_code == 404:
        raise VideoNotFoundError(f"no video for {company_name!r}")
    if resp.status_code != 200:
        raise VideoServiceError(f"HTTP {resp.status_code}: {_error_text(resp)}")
    return resp.json()["video_url"]


def PreviewVideo(video_url: str, view_seconds: float = 10) -> None:
    """Opens the URL in Chromium with a window; a direct .webm URL shows the
    built-in player. view_seconds > 0: close after that many seconds,
    0: keep it open until Enter is pressed."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        try:
            page = browser.new_page()
            page.goto(video_url)
            if view_seconds > 0:
                try:
                    page.wait_for_timeout(view_seconds * 1000)
                except PlaywrightError:
                    pass  # the user closed the window earlier, that is fine
            else:
                try:
                    input("  Press Enter to close the preview...")
                except EOFError:  # no console (stdin closed)
                    pass
        finally:
            browser.close()
