"""
One-off clean-up: remove the fake 'Rating only (no review)' rows that older versions wrote into the
'Positive Reviews' / 'Negative Reviews' sheets of existing workbooks.

  python tools/clean_placeholder_rows.py --dry-run     # report only, change nothing
  python tools/clean_placeholder_rows.py               # back up reports/, then clean them

* Every workbook is copied to backups/reports_<timestamp>/ BEFORE anything is changed.
* Only rows named 'Rating only (no review)' are removed. Genuine reviews, the Star Ratings sheet,
  keyword sheet and Time sheet are left as they are.
* Workbooks that contain no placeholder rows are not touched at all.
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openpyxl import load_workbook  # noqa: E402

from fk_common import BASE_DIR, load_config, resolve_dir  # noqa: E402
from fk_excel import (LEGACY_PLACEHOLDER_NAME, NEGATIVE_SHEET, POSITIVE_SHEET, STARS,  # noqa: E402
                      _write_review_sheet)

SHEETS = ((POSITIVE_SHEET, "Positive Reviews (4-5 star)", (5, 4)),
          (NEGATIVE_SHEET, "Negative Reviews (1-3 star)", (3, 2, 1)))


def count_placeholders(wb):
    n = 0
    for name, _, _ in SHEETS:
        if name in wb.sheetnames:
            n += sum(1 for r in wb[name].iter_rows(min_row=5, max_col=1, values_only=True)
                     if r and str(r[0]).strip() == LEGACY_PLACEHOLDER_NAME)
    return n


def latest_counts(wb):
    """Latest verified aggregate counts from the Star Ratings sheet ({} if N/A)."""
    if "Star Ratings" not in wb.sheetnames:
        return {}
    ws = wb["Star Ratings"]
    col = max((c for c in range(2, ws.max_column + 1) if ws.cell(4, c).value not in (None, "")), default=None)
    if col is None:
        return {}
    vals = [ws.cell(5 + i, col).value for i in range(len(STARS))]
    return dict(zip(STARS, vals)) if all(isinstance(v, (int, float)) for v in vals) else {}


def clean_workbook(path):
    """Strip placeholder rows from one workbook (in place, atomic save). Returns rows removed."""
    wb = load_workbook(path)
    product = (wb["Star Ratings"]["A1"].value or "").replace("Product Name: ", "") if "Star Ratings" in wb.sheetnames else path.stem
    run_date = datetime.now().strftime("%Y-%m-%d")
    counts, removed = latest_counts(wb), 0
    for name, subtitle, stars in SHEETS:
        if name in wb.sheetnames:
            _, r = _write_review_sheet(wb, name, subtitle, product, run_date, [], stars, counts)
            removed += r
    tmp = Path(str(path) + ".tmp")
    wb.save(tmp)
    tmp.replace(path)
    return removed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="only report what would be removed")
    ap.add_argument("--dir", help="reports folder (default: output_dir from fk_config.json)")
    args = ap.parse_args(argv)

    out_dir = Path(args.dir) if args.dir else resolve_dir(load_config()[0]["output_dir"])
    files = sorted(out_dir.glob("*.xlsx"))
    todo = []
    for f in files:
        n = count_placeholders(load_workbook(f))
        print(f"{f.name}: {n} placeholder row(s)")
        if n:
            todo.append(f)
    if args.dry_run or not todo:
        print("Dry run - nothing changed." if args.dry_run else "Nothing to clean.")
        return 0

    backup = BASE_DIR / "backups" / f"reports_{datetime.now():%Y%m%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, backup / f.name)
    print(f"Backed up {len(files)} workbook(s) to {backup}")
    for f in todo:
        print(f"cleaned {f.name}: removed {clean_workbook(f)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
