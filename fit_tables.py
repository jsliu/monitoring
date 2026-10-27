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
# Grafana's grid: h units are 38 * h - 8 px (30 px each, 8 px gaps). Measured on
# the dashboard: the title and padding above the header take 39 px, the header
# row 36, the bottom padding 8; a body row is 36 px ("sm"), 42 ("md"), 48 ("lg").
# Run ./run.sh fit again when the number of positions changes.
GRID_PX, GAP_PX, CHROME_PX = 38, 8, 39 + 36 + 8 + 4  # +4: never a 1 px scrollbar
ROW_PX = {"sm": 36, "md": 42, "lg": 48}
MIN_H = 4


def table_height(rows: int, cell_height: str = "sm") -> int:
    """Grid units that show `rows` table rows without a scrollbar or spare space."""
    px = CHROME_PX + ROW_PX.get(cell_height, 36) * rows
    return max(MIN_H, math.ceil((px + GAP_PX) / GRID_PX))


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
    """Keep the order, x and width; stretch each band (panels that shared a y)
    to its tallest panel so bottoms line up, and lay the bands and section rows
    top to bottom."""
    y, band_y, band = 0, None, []

    def close_band() -> int:
        h = max((p["gridPos"]["h"] for p in band), default=0)
        for p in band:
            p["gridPos"]["h"] = h
        band.clear()
        return h

    for panel in panels:
        pos = panel["gridPos"]
        if panel["type"] == "row":
            y += close_band()
            band_y = None
            pos["y"] = y
            y += 1
            continue
        if pos["y"] != band_y:  # a new band: compared with the original y
            y += close_band()
            band_y = pos["y"]
        pos["y"] = y
        band.append(panel)
    close_band()


def fit(path: Path, dry_run: bool) -> None:
    dashboard = json.loads(path.read_text())
    for panel in dashboard["panels"]:
        if panel["type"] == "table":
            rows = row_count(panel)
            if not rows:  # no data now (process down, nothing proposed): keep the height
                print(f"  {panel['title'][:58]:58}   - no data, h {panel['gridPos']['h']} kept")
                continue
            footer = panel.get("options", {}).get("footer", {}).get("show", False)
            cell = panel.get("options", {}).get("cellHeight", "sm")
            panel["gridPos"]["h"] = table_height(rows + footer, cell)
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
