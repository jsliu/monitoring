"""Size every dashboard table to show all its rows, so tables never scroll and
the page does instead; then re-stack the panels. Grafana has no auto-height for
table panels, so run this when the number of positions changes:

    ./run.sh fit            (or: python3 fit_tables.py [--dry-run])

Row counts come from Prometheus' current data. Stdlib only."""

from __future__ import annotations

import argparse
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASHBOARDS = sorted((ROOT / "grafana" / "dashboards" / "thetagang").glob("*.json"))
PROMETHEUS = "http://127.0.0.1:9090"
# Grafana's grid: a unit is 30 px plus an 8 px gap; a "sm" table row is 36 px,
# and the panel title, header row and padding take about 100 px
GRID_PX, ROW_PX, CHROME_PX = 38, 36, 100
SPARE_ROWS = 1  # one empty row of headroom, so a new position doesn't scroll at once
MIN_H = 4


def table_height(rows: int) -> int:
    """Grid units that show `rows` table rows without a scrollbar."""
    return max(MIN_H, math.ceil((CHROME_PX + ROW_PX * (rows + SPARE_ROWS)) / GRID_PX))


def query(expr: str) -> list[dict]:
    url = f"{PROMETHEUS}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)["data"]["result"]


def row_count(panel: dict) -> int:
    """Rows the table shows: distinct join / pivot keys, else the most series."""
    results = [query(t["expr"]) for t in panel.get("targets", []) if t.get("expr")]
    key = None
    for t in panel.get("transformations", []):
        options = t.get("options", {})
        key = options.get("byField") or options.get("rowField") or key
    if key:
        return len({r["metric"].get(key) for result in results for r in result})
    return max((len(result) for result in results), default=0)


def restack(panels: list[dict]) -> None:
    """Keep the order, x and width; give each band (panels that shared a y) the
    height of its tallest panel and lay the bands and section rows top to bottom."""
    y, band_y, band_h = 0, None, 0
    for panel in panels:
        pos = panel["gridPos"]
        if panel["type"] == "row":
            y += band_h
            band_y, band_h = None, 0
            pos["y"] = y
            y += 1
            continue
        if pos["y"] != band_y:  # a new band: compared with the original y
            y += band_h
            band_y, band_h = pos["y"], 0
        pos["y"] = y
        band_h = max(band_h, pos["h"])


def fit(path: Path, dry_run: bool) -> None:
    dashboard = json.loads(path.read_text())
    for panel in dashboard["panels"]:
        if panel["type"] == "table":
            rows = row_count(panel)
            panel["gridPos"]["h"] = table_height(rows)
            print(f"  {panel['title'][:58]:58} {rows:3} rows -> h {panel['gridPos']['h']}")
    restack(dashboard["panels"])
    if not dry_run:
        path.write_text(json.dumps(dashboard, indent=1) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the sizes only")
    args = parser.parse_args()
    for path in DASHBOARDS:
        print(path.relative_to(ROOT))
        fit(path, args.dry_run)


if __name__ == "__main__":
    main()
