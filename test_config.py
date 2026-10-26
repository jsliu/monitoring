"""Checks of the monitoring stack's config. Text and JSON only, no YAML library or Docker:
python3 -m pytest (any Python with pytest)."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
GRAFANA = ROOT / "grafana"
SCRIPT = ROOT / "run.sh"
ALERTING = GRAFANA / "provisioning" / "alerting"


def _json(path):
    return json.loads(path.read_text())


def _rules():
    return [r for g in _json(ALERTING / "rules.yaml")["groups"] for r in g["rules"]]


def test_every_provisioning_file_and_dashboard_parses_as_json():
    files = [p for p in GRAFANA.rglob("*") if p.is_file() and p.name != "contact-point.yaml"]  # that one is generated
    assert len(files) == 6 and all(isinstance(_json(p), dict) for p in files)


def test_each_project_has_its_own_grafana_folder():
    providers = _json(GRAFANA / "provisioning" / "dashboards" / "dashboards.yaml")["providers"]
    folders = {p["folder"]: p["options"]["path"] for p in providers}
    assert folders == {"Intraday": "/var/lib/grafana/dashboards/intraday",
                       "thetagang": "/var/lib/grafana/dashboards/thetagang"}
    for path in folders.values():  # each folder's directory holds its dashboards, nothing else
        assert list((GRAFANA / "dashboards" / Path(path).name).glob("*.json"))


def test_every_alert_window_has_its_route_and_a_new_york_time_interval():
    notifications = _json(ALERTING / "notifications.yaml")
    intervals = {m["name"]: m for m in notifications["muteTimes"]}
    routes = {r["object_matchers"][0][2]: r for r in notifications["policies"][0]["routes"]}
    rules = _rules()

    assert len(rules) == 14  # 6 intraday + 8 thetagang
    for rule in rules:
        window = rule["labels"]["window"]
        assert routes[window]["active_time_intervals"] == [window]
        assert intervals[window]["time_intervals"][0]["location"] == "America/New_York"


def test_thetagang_alerts_query_only_thetagang_metrics():
    groups = _json(ALERTING / "rules.yaml")["groups"]
    rules = [r for g in groups if g["name"] == "thetagang" for r in g["rules"]]
    exprs = [d["model"]["expr"] for r in rules for d in r["data"] if "expr" in d["model"]]
    assert len(rules) == 8 and all(r["labels"]["window"] == "thetagang" for r in rules)
    assert all(re.findall(r"\bthetagang_[a-z_]+|up\{job=\"thetagang(-strategy)?\"\}", e) for e in exprs)


def test_every_published_port_is_localhost_only_and_history_is_two_years():
    compose = (ROOT / "docker-compose.yml").read_text()
    published = re.findall(r'^\s*-\s*"([^"]+:\d+)"\s*$', compose, flags=re.M)
    ports = [p for p in published if re.fullmatch(r"[\d.]*:?\d+:\d+", p)]
    assert len(ports) == 2 and all(p.startswith("127.0.0.1:") for p in ports)
    assert "--storage.tsdb.retention.time=2y" in compose
    scrape = (ROOT / "prometheus.yml").read_text()
    for target in ("host.docker.internal:9102", "host.docker.internal:9101", "host.docker.internal:9464",
                   "host.docker.internal:9465"):
        assert f'"{target}"' in scrape


def test_secrets_are_git_ignored_and_the_example_is_not():
    ignore = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignore and "!.env.example" in ignore
    assert "grafana/provisioning/alerting/contact-point.yaml" in ignore
    assert "grafana/provisioning/alerting/contact-point.yaml.tmp" in ignore  # an interrupted render's leftover
    assert (ROOT / ".env.example").exists()


def _render(tmp_path, token="123456:AAH-abc_def", chat="-1001234567", password="pw"):
    env = tmp_path / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat}\nGF_SECURITY_ADMIN_PASSWORD={password}\n")
    out = tmp_path / "contact-point.yaml"
    proc = subprocess.run(["bash", str(SCRIPT), "render"], capture_output=True, text=True,
                          env={**os.environ, "MONITORING_ENV_FILE": str(env), "CONTACT_POINT_FILE": str(out)})
    return proc, out


def test_the_contact_point_is_rendered_with_the_chat_id_quoted_and_private(tmp_path):
    proc, out = _render(tmp_path)

    assert proc.returncode == 0, proc.stderr
    text = out.read_text()
    assert "chatid: '-1001234567'" in text and "bottoken: '123456:AAH-abc_def'" in text  # quoted: Grafana needs strings
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


@pytest.mark.parametrize("token, chat", [("not-a-token", "1"), ("123:abc", "@mychannel"), ("123:abc", "")])
def test_a_malformed_token_or_chat_id_is_refused_before_anything_is_written(tmp_path, token, chat):
    proc, out = _render(tmp_path, token, chat)

    assert proc.returncode != 0 and "error:" in proc.stderr and not out.exists()


def test_env_is_read_as_text_never_run_as_shell_code(tmp_path):
    pwned = tmp_path / "pwned"
    proc, out = _render(tmp_path, "123456:AAH-abc", "42", f"pa ss$(touch {pwned})$HOME")

    assert proc.returncode == 0, proc.stderr
    assert not pwned.exists() and "chatid: '42'" in out.read_text()


def test_grafana_gets_only_its_password():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "env_file" not in compose and "TELEGRAM" not in compose
    assert "GF_SECURITY_ADMIN_PASSWORD: ${GF_SECURITY_ADMIN_PASSWORD" in compose


def test_env_example_says_the_grafana_password_applies_at_the_first_start_only():
    text = (ROOT / ".env.example").read_text()
    assert "first start" in text and "$" in text  # also warns about shell characters


def test_the_intraday_drawdown_panel_says_trailing_and_scales_its_resolution_with_the_range():
    dashboard = _json(GRAFANA / "dashboards" / "intraday" / "portfolio.json")
    (panel,) = [p for p in dashboard["panels"] if p.get("title", "").lower().startswith("drawdown")]
    assert "trailing" in panel["title"] and "[$__range:$__interval]" in panel["targets"][0]["expr"]


def test_the_intraday_daily_pnl_panel_shows_one_bar_per_new_york_close():
    dashboard = _json(GRAFANA / "dashboards" / "intraday" / "portfolio.json")
    (panel,) = [p for p in dashboard["panels"] if p.get("title", "").startswith("Daily P&L")]
    assert panel["type"] == "barchart" and "ib_close_pnl_usd" in panel["targets"][0]["expr"]
    assert panel["targets"][0].get("instant") is True


def test_the_thetagang_tiles_show_the_latest_value_in_any_time_range():
    dashboard = _json(GRAFANA / "dashboards" / "thetagang" / "thetagang.json")
    tiles = [p for p in dashboard["panels"] if p["type"] == "stat"]
    assert len(tiles) == 12 and all(t["instant"] and not t["range"] for p in tiles for t in p["targets"])


def test_a_stopped_project_never_alerts_only_one_that_stops_after_running():
    """Projects run independently: a "down" alert fires only if the target was up in the last 8 hours, so a project
    that isn't run that day (intraday without thetagang, or the reverse) never alerts."""
    rules = {r["uid"]: r for r in _rules()}
    for uid, job in (("im-node-down", "node"), ("im-ib-disconnected", "exporter"), ("tg-bot-down", "thetagang"),
                     ("tg-strategy-down", "thetagang-strategy")):
        (expr,) = [d["model"]["expr"] for d in rules[uid]["data"] if d["refId"] == "A"]
        (cond,) = [d["model"]["conditions"][0]["evaluator"] for d in rules[uid]["data"] if d["refId"] == "C"]
        assert "max_over_time(" in expr and "[8h" in expr and f'up{{job="{job}"}}' in expr
        assert cond == {"type": "gt", "params": [0.5]}
    for rule in rules.values():  # every other rule is silent without data
        assert rule["noDataState"] == "OK"


def test_fit_sizes_tables_to_their_rows_and_restacks_the_bands():
    import fit_tables as ft

    assert ft.table_height(0) == ft.MIN_H and ft.table_height(40) > ft.table_height(10)
    rows_px = ft.GRID_PX * ft.table_height(12) - 8
    assert rows_px >= ft.CHROME_PX + ft.ROW_PX * 12  # all 12 rows fit
    panels = [
        {"type": "row", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
        {"type": "table", "gridPos": {"x": 0, "y": 1, "w": 12, "h": 9}},
        {"type": "stat", "gridPos": {"x": 12, "y": 1, "w": 12, "h": 4}},  # same band
        {"type": "table", "gridPos": {"x": 0, "y": 5, "w": 24, "h": 20}},  # next band
        {"type": "row", "gridPos": {"x": 0, "y": 30, "w": 24, "h": 1}},
    ]
    ft.restack(panels)
    assert [p["gridPos"]["y"] for p in panels] == [0, 1, 1, 10, 30]
