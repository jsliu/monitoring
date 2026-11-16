"""Checks of the monitoring stack's config. Text and JSON only, no YAML library or Docker:
python3 -m pytest (any Python with pytest)."""

import json
import os
import re
import stat
import subprocess
import time
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
    assert len(files) == 10 and all(isinstance(_json(p), dict) for p in files)


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

    assert len(rules) == 15  # 6 intraday + 9 thetagang
    for rule in rules:
        window = rule["labels"]["window"]
        assert routes[window]["active_time_intervals"] == [window]
        assert intervals[window]["time_intervals"][0]["location"] == "America/New_York"


def test_thetagang_alerts_query_only_thetagang_metrics():
    groups = _json(ALERTING / "rules.yaml")["groups"]
    rules = [r for g in groups if g["name"] == "thetagang" for r in g["rules"]]
    exprs = [d["model"]["expr"] for r in rules for d in r["data"] if "expr" in d["model"]]
    assert len(rules) == 9 and all(r["labels"]["window"] == "thetagang" for r in rules)
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


def test_telegram_messages_use_the_readable_template(tmp_path):
    proc, out = _render(tmp_path)
    templates = _json(ALERTING / "templates.yaml")["templates"]

    assert proc.returncode == 0, proc.stderr
    assert """message: '{{ template "telegram.readable" . }}'""" in out.read_text()
    assert [t["name"] for t in templates] == ["telegram.readable"]
    assert '{{ define "telegram.readable" -}}' in templates[0]["template"]


def test_every_alert_says_which_account_it_is_about():
    # The message header is "<PROJECT> · <ACCOUNT> account": intraday trades only the DU... paper login,
    # thetagang the live U... account (its metrics' account label, 2026-11-01).
    expected = {"intraday": "paper", "thetagang": "live"}
    for group in _json(ALERTING / "rules.yaml")["groups"]:
        assert {r["labels"]["account"] for r in group["rules"]} == {expected[group["name"]]}, group["name"]
    assert "toUpper .Labels.account" in _json(ALERTING / "templates.yaml")["templates"][0]["template"]


def test_alert_summaries_use_only_functions_grafana_has():
    # Grafana's annotation templates have no arithmetic: "mul" left the stress-loss summary unrendered (2026-11-01)
    for rule in _rules():
        assert not re.search(r"\{\{[^}]*\b(mul|div|add|sub)\b", rule["annotations"]["summary"]), rule["title"]


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
    """Its admin password and the journal's read-only login; never Telegram's."""
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "env_file" not in compose and "TELEGRAM" not in compose
    assert "GF_SECURITY_ADMIN_PASSWORD: ${GF_SECURITY_ADMIN_PASSWORD" in compose
    assert re.findall(r"^\s+([A-Z_]*PASSWORD):", compose, re.M) == [
        "GF_SECURITY_ADMIN_PASSWORD", "JOURNAL_GRAFANA_PASSWORD"]


def test_env_example_says_the_grafana_password_applies_at_the_first_start_only():
    text = (ROOT / ".env.example").read_text()
    assert "first start" in text and "$" in text  # also warns about shell characters


def test_env_example_configures_the_expected_thetagang_identity():
    text = (ROOT / ".env.example").read_text()
    assert "THETAGANG_EXPECTED_IB_ACCOUNT_ID=" in text
    assert "THETAGANG_EXPECTED_IB_CLIENT_ID=" in text


def _target(job, health="up"):
    return {"labels": {"job": job}, "health": health, "scrapeUrl": f"http://{job}"}


def _health_metric(role="monitor", client_id="42", account="U123", value="1", timestamp=None):
    return {
        "metric": {"role": role, "client_id": client_id, "account": account},
        "value": [timestamp if timestamp is not None else time.time(), value],
    }


def _run_monitoring_script(
    tmp_path, command, targets, metrics, account="U123", running_services=("thetagang-monitor",)
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=123456:AAH-abc_def\n"
        "TELEGRAM_CHAT_ID=42\n"
        "GF_SECURITY_ADMIN_PASSWORD=pw\n"
        f"THETAGANG_EXPECTED_IB_ACCOUNT_ID={account}\n"
        "THETAGANG_EXPECTED_IB_CLIENT_ID=41\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl = fake_bin / "curl"
    curl.write_text(
        """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
for arg in args:
    if arg.startswith("query="):
        with open(os.environ["PROM_QUERY_LOG"], "a") as log:
            log.write(arg[len("query="):] + "\\n")
if args[-1].endswith("/api/v1/targets"):
    data = {"activeTargets": json.loads(os.environ["PROM_TARGETS"])}
else:
    data = {"resultType": "vector", "result": json.loads(os.environ["PROM_METRICS"])}
print(json.dumps({"status": "success", "data": data}))
"""
    )
    curl.chmod(0o755)
    query_log = tmp_path / "queries"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
with open(os.environ["DOCKER_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
if "ps" in args:
    print(os.environ["RUNNING_SERVICES"])
"""
    )
    docker.chmod(0o755)
    return subprocess.run(
        ["bash", str(SCRIPT), command],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "MONITORING_ENV_FILE": str(env_file),
            "CONTACT_POINT_FILE": str(tmp_path / "contact-point.yaml"),
            "PROM_TARGETS": json.dumps(targets),
            "PROM_METRICS": json.dumps(metrics),
            "PROM_QUERY_LOG": str(query_log),
            "RUNNING_SERVICES": "\n".join(running_services),
            "DOCKER_LOG": str(tmp_path / "docker.log"),
        },
    ), query_log, tmp_path


def test_health_accepts_a_matching_monitor_and_an_absent_strategy_target(tmp_path):
    proc, query_log, work = _run_monitoring_script(
        tmp_path, "health", [_target("thetagang")], [_health_metric()]
    )

    assert proc.returncode == 0, proc.stderr
    assert "thetagang-strategy: not expected" in proc.stdout
    assert 'account="U123",role="monitor",client_id="42"' in query_log.read_text()
    assert "--profile trading ps --status running --services" in (work / "docker.log").read_text()


def test_health_treats_a_down_target_as_not_expected_when_trade_service_is_stopped(tmp_path):
    proc, _, _ = _run_monitoring_script(
        tmp_path,
        "health",
        [_target("thetagang"), _target("thetagang-strategy", "down")],
        [_health_metric()],
    )

    assert proc.returncode == 0, proc.stderr
    assert "thetagang-strategy: not expected" in proc.stdout


def test_health_rejects_a_down_strategy_target_when_trade_service_is_running(tmp_path):
    proc, _, _ = _run_monitoring_script(
        tmp_path,
        "health",
        [_target("thetagang"), _target("thetagang-strategy", "down")],
        [_health_metric()],
        running_services=("thetagang-monitor", "thetagang-nt"),
    )

    assert proc.returncode != 0
    assert "thetagang-strategy" in proc.stderr


def test_health_rejects_a_missing_strategy_target_when_trade_service_is_running(tmp_path):
    proc, _, _ = _run_monitoring_script(
        tmp_path,
        "health",
        [_target("thetagang")],
        [_health_metric()],
        running_services=("thetagang-monitor", "thetagang-nt"),
    )

    assert proc.returncode != 0
    assert "thetagang-strategy" in proc.stderr


def test_health_rejects_a_down_monitor_target_when_monitor_service_is_running(tmp_path):
    proc, _, _ = _run_monitoring_script(
        tmp_path, "health", [_target("thetagang", "down")], []
    )

    assert proc.returncode != 0
    assert "thetagang" in proc.stderr


def test_health_rejects_a_missing_monitor_target_when_monitor_service_is_running(tmp_path):
    proc, _, _ = _run_monitoring_script(tmp_path, "health", [], [])

    assert proc.returncode != 0
    assert "thetagang" in proc.stderr


def test_health_accepts_matching_monitor_and_strategy_targets(tmp_path):
    proc, query_log, _ = _run_monitoring_script(
        tmp_path,
        "health",
        [_target("thetagang"), _target("thetagang-strategy")],
        [_health_metric(), _health_metric("trade", "41")],
        running_services=("thetagang-monitor", "thetagang-nt"),
    )

    assert proc.returncode == 0, proc.stderr
    assert 'account="U123",role="trade",client_id="41"' in query_log.read_text()


def test_health_rejects_a_mismatched_trade_identity(tmp_path):
    proc, _, _ = _run_monitoring_script(
        tmp_path,
        "health",
        [_target("thetagang"), _target("thetagang-strategy")],
        [_health_metric(), _health_metric("trade", "42")],
        running_services=("thetagang-monitor", "thetagang-nt"),
    )

    assert proc.returncode != 0
    assert "thetagang-strategy" in proc.stderr


@pytest.mark.parametrize(
    "metric",
    [
        _health_metric(account="U999"),
        _health_metric(client_id="43"),
        _health_metric(role="trade"),
        _health_metric(value="0"),
    ],
)
def test_health_rejects_wrong_identity_or_unhealthy_metrics(tmp_path, metric):
    proc, _, _ = _run_monitoring_script(tmp_path, "health", [_target("thetagang")], [metric])

    assert proc.returncode != 0
    assert "thetagang" in proc.stderr


def test_health_rejects_a_missing_metric_for_a_running_target(tmp_path):
    proc, _, _ = _run_monitoring_script(tmp_path, "health", [_target("thetagang")], [])

    assert proc.returncode != 0
    assert "thetagang" in proc.stderr


def test_health_rejects_a_stale_metric_for_a_running_target(tmp_path):
    proc, _, _ = _run_monitoring_script(
        tmp_path, "health", [_target("thetagang")], [_health_metric(timestamp=time.time() - 120)]
    )

    assert proc.returncode != 0
    assert "thetagang" in proc.stderr


def test_health_escapes_account_label_values_in_prometheus_queries(tmp_path):
    account = 'U"12\\3'
    proc, query_log, _ = _run_monitoring_script(
        tmp_path,
        "health",
        [_target("thetagang")],
        [_health_metric(account=account)],
        account=account,
    )

    assert proc.returncode == 0, proc.stderr
    assert 'account="U\\"12\\\\3"' in query_log.read_text()


def test_up_starts_monitoring_even_when_the_bot_health_check_fails(tmp_path):
    proc, query_log, work = _run_monitoring_script(
        tmp_path, "up", [_target("thetagang")], []
    )

    assert proc.returncode == 0, proc.stderr
    assert "up -d" in (work / "docker.log").read_text()
    assert query_log.exists()


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
    tiles = [p for p in dashboard["panels"] if p["type"] in ("stat", "gauge")]
    assert len(tiles) == 20
    for p in tiles:  # an instant value, or a sparkline pinned to the last 24 h whatever range is picked
        assert all(t["instant"] and not t["range"] for t in p["targets"]) or p.get("timeFrom") == "24h", p["title"]


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


def test_thetagang_session_health_alert_checks_only_recently_active_roles():
    rules = {r["uid"]: r for r in _rules()}
    rule = rules["tg-ib-session-unhealthy"]
    (expr,) = [d["model"]["expr"] for d in rule["data"] if d["refId"] == "A"]

    branches = expr.split(" or ")
    assert len(branches) == 2
    role_by_job = {}
    for branch in branches:
        (job,) = re.findall(r'up\{job="([^"]+)"\}\[8h\]', branch)
        (role,) = re.findall(
            r'thetagang_ib_session_healthy\{role="([^"]+)"\}\s*==\s*1', branch
        )
        role_by_job[job] = role
    assert role_by_job == {"thetagang": "monitor", "thetagang-strategy": "trade"}
    assert expr.count("unless on(job, instance)") == 2
    assert rule["labels"] == {"window": "thetagang", "account": "live"}
    assert rule["noDataState"] == "OK"
    assert "or vector(0)" not in expr


def test_fit_sizes_tables_to_their_rows_and_restacks_the_bands():
    import fit_tables as ft

    assert ft.table_height(0) == ft.MIN_H and ft.table_height(40) > ft.table_height(10)
    for cell, row_px in ft.ROW_PX.items():
        panel_px = ft.GRID_PX * ft.table_height(12, cell) - ft.GAP_PX
        needed = ft.CHROME_PX + row_px * 12
        assert needed <= panel_px < needed + ft.GRID_PX  # all 12 rows fit, < 1 row spare
    panels = [
        {"type": "row", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
        {"type": "table", "gridPos": {"x": 0, "y": 1, "w": 12, "h": 9}},
        {"type": "stat", "gridPos": {"x": 12, "y": 1, "w": 12, "h": 4}},  # same band
        {"type": "table", "gridPos": {"x": 0, "y": 5, "w": 24, "h": 20}},  # next band
        {"type": "row", "gridPos": {"x": 0, "y": 30, "w": 24, "h": 1}},
    ]
    ft.restack(panels)
    assert [p["gridPos"]["y"] for p in panels] == [0, 1, 1, 10, 30]


def test_thetagang_transforms_are_ones_grafana_knows():
    """Grafana silently skips an unknown transform id (configFromQuery is really configFromData)."""
    known = {"configFromData", "filterFieldsByName", "groupingToMatrix", "joinByField", "merge", "organize",
             "rowsToFields", "sortBy", "calculateField", "convertFieldType", "reduce", "seriesToRows", "labelsToFields"}
    for path in (GRAFANA / "dashboards" / "thetagang").glob("*.json"):
        used = {t["id"] for p in _json(path)["panels"] for t in p.get("transformations", [])}
        assert used <= known, (path.name, used - known)


def test_thetagang_regexes_are_javascript():
    """Grafana runs mapping and field-name regexes in the browser: an inline flag like (?i) is Python/Go only and
    crashes the whole table (write /pattern/i instead)."""
    import re as _re

    for path in (GRAFANA / "dashboards" / "thetagang").glob("*.json"):
        bad = _re.findall(r'"pattern": "([^"]*\(\?[imsx][^"]*)"', path.read_text())
        assert not bad, (path.name, bad)
