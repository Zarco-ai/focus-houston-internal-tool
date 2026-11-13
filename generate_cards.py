#!/usr/bin/env python3
"""Generate a batch of card numbers for mothers.

    python generate_cards.py              200 new numbers (the default)
    python generate_cards.py --count 100  a different amount

Writes two files into instance/card_lists/ (git ignores instance/):

    cards-batch-N.csv         the list the app loads (Phase 5). Numbers only.
    cards-batch-N-print.html  open in a browser and print. One row per
                              number with blank columns for the mother's name
                              and the date it was given out.

Every earlier batch in that folder is read first, so a new batch never reuses
a number. Nothing is ever overwritten.

The printed sheet is the ONLY place a mother's name and her number meet. It
is handwritten, kept locked in the office, and never typed into the app,
photographed, or scanned. See app/services/card_numbers.py for the format.
"""

import argparse
import csv
import datetime
import os
import sys
from zoneinfo import ZoneInfo

from app.services import card_numbers

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(HERE, "instance", "card_lists")
ROWS_PER_PAGE = 25

# The clock module (Phase 1) does not exist yet. This is a local command run
# on a laptop, but it still asks for Houston's date explicitly rather than
# trusting whatever zone the machine is set to.
HOUSTON = ZoneInfo("America/Chicago")


def existing_batches():
    """[(batch_number, [codes...]), ...] for every batch already written."""
    found = []
    if not os.path.isdir(OUTPUT_DIR):
        return found
    for name in sorted(os.listdir(OUTPUT_DIR)):
        if not (name.startswith("cards-batch-") and name.endswith(".csv")):
            continue
        number = int(name[len("cards-batch-"):-len(".csv")])
        with open(os.path.join(OUTPUT_DIR, name), newline="", encoding="utf-8") as handle:
            codes = [row["card_number"] for row in csv.DictReader(handle)]
        found.append((number, codes))
    return found


def write_csv(path, batch_number, codes, generated_on):
    with open(path, "x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["list_position", "card_number", "batch", "generated_on"])
        for position, code in enumerate(codes, start=1):
            writer.writerow([position, code, batch_number, generated_on.isoformat()])


def write_printable(path, batch_number, codes, generated_on):
    pages = [codes[i:i + ROWS_PER_PAGE] for i in range(0, len(codes), ROWS_PER_PAGE)]
    page_html = []
    for page_index, page in enumerate(pages):
        rows = "\n".join(
            f"<tr><td class=pos>{page_index * ROWS_PER_PAGE + i}</td>"
            f"<td class=code>{code}</td><td></td><td></td></tr>"
            for i, code in enumerate(page, start=1)
        )
        page_html.append(f"""
<section class=page>
  <header>
    <h1>FOCUS Houston — card numbers, batch {batch_number}</h1>
    <p class=warn>CONFIDENTIAL. Keep this sheet locked in the office.
    Never type a name into the app, and never photograph or scan this page.</p>
    <p class=meta>Generated {generated_on:%B %-d, %Y} · page {page_index + 1} of {len(pages)} ·
    give out numbers from the top down</p>
  </header>
  <table>
    <thead><tr><th>#</th><th>Card number</th><th>Mother's name</th><th>Date given</th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
</section>""")

    document = f"""<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<title>FOCUS card numbers — batch {batch_number}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; margin: 0; color: #111; }}
  .page {{ padding: 0.6in 0.7in; page-break-after: always; }}
  .page:last-child {{ page-break-after: auto; }}
  h1 {{ font-size: 16pt; margin: 0 0 4pt; }}
  .warn {{ font-weight: 700; margin: 0 0 2pt; }}
  .meta {{ color: #444; margin: 0 0 10pt; font-size: 10pt; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ border: 1px solid #888; padding: 0 8pt; height: 0.34in; text-align: left; }}
  th {{ font-size: 10pt; background: #eee; }}
  td.pos {{ width: 0.4in; color: #666; font-size: 9pt; }}
  td.code {{ width: 1.3in; font: 600 15pt/1 ui-monospace, Menlo, Consolas, monospace; letter-spacing: 2pt; }}
  th:nth-child(4), td:nth-child(4) {{ width: 1.3in; }}
  @media print {{ .page {{ padding: 0; }} @page {{ margin: 0.6in; }} }}
</style>
</head>
<body>
{"".join(page_html)}
</body>
</html>
"""
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(document)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=200,
                        help="how many new numbers (default 200)")
    args = parser.parse_args()

    batches = existing_batches()
    already_issued = [code for _, codes in batches for code in codes]
    batch_number = max((number for number, _ in batches), default=0) + 1

    try:
        codes = card_numbers.generate(args.count, existing=already_issued)
    except card_numbers.CardNumberError as error:
        print(f"Could not generate card numbers:\n  {error}", file=sys.stderr)
        return 1

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    generated_on = datetime.datetime.now(HOUSTON).date()
    csv_path = os.path.join(OUTPUT_DIR, f"cards-batch-{batch_number}.csv")
    print_path = os.path.join(OUTPUT_DIR, f"cards-batch-{batch_number}-print.html")

    write_csv(csv_path, batch_number, codes, generated_on)
    write_printable(print_path, batch_number, codes, generated_on)

    print(f"Batch {batch_number}: {len(codes)} new card numbers "
          f"({len(already_issued)} issued in earlier batches were skipped).")
    print(f"  list for the app : {csv_path}")
    print(f"  print this       : {print_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
