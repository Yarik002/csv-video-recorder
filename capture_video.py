"""Reads the CSV and records one video per row with qscript.CaptureVideo_Magento.

Usage:
    python capture_video.py                      # input.csv -> ./video
    python capture_video.py my.csv --video-dir out --headless
"""

import argparse
import csv
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Record a browser video for every CSV row.")
    parser.add_argument("csv", nargs="?", default="input.csv", help="input CSV (default: input.csv)")
    parser.add_argument("--video-dir", help=f"output folder (default: {qscript.PATH_TO_VIDEO_FOLDER})")
    parser.add_argument("--headless", action="store_true",
                        help="hide the browser window (DuckDuckGo blocks headless search)")
    args = parser.parse_args(argv)

    if args.video_dir:
        qscript.PATH_TO_VIDEO_FOLDER = args.video_dir
    if args.headless:
        qscript.HEADLESS = True

    try:
        rows = read_rows(args.csv)
    except (OSError, ValueError) as exc:
        print(f"Cannot read CSV: {exc}", file=sys.stderr)
        return 2

    names = [r["Company Name"] for r in rows]
    for name in sorted({n for n in names if names.count(n) > 1}):
        print(f"Warning: company {name!r} appears more than once, its video will be overwritten")

    failed = 0
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
        else:
            failed += 1
            print("  FAILED")

    print(f"Done: {len(rows) - failed} of {len(rows)} videos saved.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
