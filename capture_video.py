"""Reads the CSV and records one video per row with qscript.CaptureVideo_Magento.

Usage:
    python capture_video.py                      # input.csv -> ./video
    python capture_video.py my.csv --video-dir out --headless
    python capture_video.py --server http://127.0.0.1:8000   # + upload and preview (Task 2)
    python capture_video.py --zoom      # the scenario zooms in on the search box (Internship task 2)

F5 during a recording toggles a zoom at the mouse point; the events go to
<video>.zoom.json, and apply_zoom.py renders them.
"""

import argparse
import csv
import math
import sys
import time

import qscript

COLUMNS = ["URL", "Company Name", "Language", "Country", "Postcode", "City", "Street", "HouseNo"]


def read_rows(csv_path):
    """Returns the rows as dicts. Raises ValueError if a column is missing."""
    # utf-8-sig also accepts a file saved by Excel with a BOM.
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";", quotechar='"')
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{csv_path}: missing columns: {', '.join(missing)}")
        rows = []
        for row in reader:
            values = {c: (row.get(c) or "").strip() for c in COLUMNS}
            if any(values.values()):  # skip empty lines
                rows.append(values)
        return rows


def _seconds(text):
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {text!r}")
    return value


def upload_and_preview(video_client, server_url, video_path, company_name, view_seconds):
    """Task 2: upload the video, log its URL, open it in Chromium.
    Returns (uploaded, previewed). Errors are printed, never raised, so the batch goes on."""
    try:
        url = video_client.UploadVideo(server_url, video_path, company_name)
    except Exception as exc:
        print(f"  upload failed: {exc}")
        return False, False
    print(f"  uploaded: {url}")
    try:
        video_client.PreviewVideo(url, view_seconds)
    except Exception as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else ""
        print(f"  preview failed: {exc.__class__.__name__}: {first_line}")
        return True, False
    return True, True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record a browser video for every CSV row.")
    parser.add_argument("csv", nargs="?", default="input.csv", help="input CSV (default: input.csv)")
    parser.add_argument("--video-dir", help=f"output folder (default: {qscript.PATH_TO_VIDEO_FOLDER})")
    parser.add_argument("--headless", action="store_true",
                        help="hide the browser window (DuckDuckGo blocks headless search)")
    parser.add_argument("--zoom", action="store_true",
                        help="the scenario zooms in on the search box while it types (writes <video>.zoom.json)")
    parser.add_argument("--server", metavar="URL",
                        help="upload every video to this web service (python web_service.py) "
                             "and preview it in Chromium, e.g. http://127.0.0.1:8000")
    parser.add_argument("--view-seconds", type=_seconds, metavar="N",
                        help="with --server: keep the preview open N seconds, "
                             "0 = until Enter (default: 10)")
    args = parser.parse_args(argv)
    if args.view_seconds is not None and not args.server:
        parser.error("--view-seconds works only with --server")

    video_client = None
    if args.server:
        # Imported only here: without --server the recorder does not need requests.
        try:
            import requests  # noqa: F401
        except ImportError:
            print("--server needs the requests package: pip install -r requirements.txt",
                  file=sys.stderr)
            return 2
        import video_client
        view_seconds = 10.0 if args.view_seconds is None else args.view_seconds

    if args.video_dir:
        qscript.PATH_TO_VIDEO_FOLDER = args.video_dir
    if args.headless:
        qscript.HEADLESS = True
    if args.zoom:
        qscript.ZOOM_IN_SCENARIO = True

    try:
        rows = read_rows(args.csv)
    except (OSError, ValueError) as exc:
        print(f"Cannot read CSV: {exc}", file=sys.stderr)
        return 2

    names = [r["Company Name"] for r in rows]
    for name in sorted({n for n in names if names.count(n) > 1}):
        print(f"Warning: company {name!r} appears more than once, its video will be overwritten")

    failed = uploaded = previewed = 0
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {row['Company Name']} ({row['City']})")
        started = time.time()
        path = qscript.CaptureVideo_Magento(
            row["URL"],
            row["Company Name"],
            row["Language"],
            row["Country"],
            row["Postcode"],
            row["City"],
            row["Street"],
            row["HouseNo"],
        )
        if path:
            print(f"  saved: {path} ({time.time() - started:.0f} s)")
            if video_client:
                up, seen = upload_and_preview(
                    video_client, args.server, path, row["Company Name"], view_seconds
                )
                uploaded += up
                previewed += seen
        else:
            failed += 1
            print("  FAILED")

    saved = len(rows) - failed
    if video_client:
        print(f"Done: {saved} of {len(rows)} videos saved, {uploaded} uploaded, {previewed} previewed.")
        return 1 if failed or uploaded < saved or previewed < saved else 0
    print(f"Done: {saved} of {len(rows)} videos saved.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
