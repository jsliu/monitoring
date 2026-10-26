# monitoring

Prometheus, Grafana and Telegram alerts for the trading projects on this Mac.
Each project only serves metrics on the host's `127.0.0.1`; this stack scrapes
them, keeps two years of history, shows the dashboards and sends the alerts.

| Prometheus job | Source | Port |
|---|---|---|
| `exporter` | `../intraday`: read-only IBKR account exporter (`./intraday/run_monitoring.sh up`) | 9102 |
| `node` | `../intraday`: momentum node, only while `run_live_momentum.sh` runs | 9101 |
| `thetagang` | `../thetagang`: thetagang-nt (`cd deploy && docker compose up -d`) | 9464 |

## Use

```console
cp .env.example .env    # once: Grafana admin password, Telegram bot token and chat id
./run.sh up             # start or update; Grafana at http://localhost:3000 (user admin)
./run.sh status         # containers, and whether Prometheus reaches each project
./run.sh test-alert     # a Telegram test message with .env's token and chat id
./run.sh down           # stop (history stays in the Docker volumes)
```

Places no orders. Every published port is bound to `127.0.0.1`.

## Layout

| Path | Contents |
|---|---|
| `docker-compose.yml` | Prometheus v3.15.1 and Grafana 13.2.4 (compose project `monitoring`) |
| `prometheus.yml` | the three scrape jobs above |
| `grafana/dashboards/intraday/portfolio.json` | Grafana folder **Intraday**: *Intraday paper book* |
| `grafana/dashboards/thetagang/thetagang.json` | Grafana folder **thetagang**: *thetagang* |
| `grafana/provisioning/` | datasource, one dashboard provider per folder, 13 alert rules (6 intraday, 7 thetagang) and their New York-time windows |
| `run.sh` | the launcher; renders the Telegram contact point from `.env` into a git-ignored file (mode 600) before Grafana starts, since Grafana rejects an env-substituted numeric chat id |
| `test_config.py` | config checks: `python3 -m pytest` with any Python that has pytest |

To add a project: give it a scrape job in `prometheus.yml`, a folder under
`grafana/dashboards/` with a matching provider in
`grafana/provisioning/dashboards/dashboards.yaml`, and optionally an alert
group in `rules.yaml` with a window in `notifications.yaml`. Then update the
counts in `test_config.py` and run `./run.sh up`.

Secrets live only in `.env` and the contact point rendered from it; both are
git-ignored. Grafana reads the admin password from `.env` only at its first
start, so change it in Grafana afterwards.
