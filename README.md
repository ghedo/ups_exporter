# ups_exporter - NUT Prometheus exporter

A Python exporter that gathers UPS data from one or more NUT servers using
`upsc` and writes it to a Prometheus textfile for the textfile collector of
`node_exporter` (or any scraper that reads the same format).

Metric names, types and defaults match `nut_exporter`. Because several servers
can be configured, every sample carries a `server` (host:port) and a `ups`
label.

Unlike `nut_exporter`, which speaks the NUT protocol through the `go.nut`
library, this exporter wraps the standard `upsc` client, which the NUT
project maintains. A single timer run serves every endpoint through one
textfile, so there is no new service or port to operate.

## Requirements

- The NUT client suite (`upsc`).
- Python 3 with the `prometheus_client` library.

## Usage

```
ups_exporter.py ENDPOINT [ENDPOINT ...] [options]
```

Endpoints use the syntax `upsname@host[:port]` (port defaults to 3493). Failed
endpoints produce an `error` sample and a non-zero exit code.

| Option | Default | Description |
| --- | --- | --- |
| `--metrics-file` | `/var/lib/prometheus/ups.prom` | textfile to write |
| `--namespace` | `network_ups_tools` | metric namespace prefix |
| `--variables` | the 7 variables below | variables to export; empty exports every numeric one |
| `--timeout` | `10` | seconds per upsc invocation |
| `--version` | | print the version and exit |

The default variables are `battery.charge`, `battery.voltage`,
`battery.voltage.nominal`, `input.voltage`, `input.voltage.nominal`, `ups.load`
and `ups.status`.

## Running

The repository provides a systemd service unit `prometheus-ups-exporter.service`
(runs the exporter once) and a timer unit `prometheus-ups-exporter.timer`:

```
install -m 0755 ups_exporter.py /usr/local/bin/
install -m 0644 prometheus-ups-exporter.* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now prometheus-ups-exporter.timer
```

The service reads the endpoints from `/etc/default/prometheus-ups-exporter` and
writes `/var/lib/prometheus/ups.prom`:

```
UPS_ENDPOINTS="ups1@ups-host-1 ups2@ups-host-2"
```

Point the `node_exporter` textfile collector at the same directory:
`--collector.textfile.directory=/var/lib/prometheus`.

## Exported metrics

- one gauge per numeric NUT variable (e.g. `network_ups_tools_battery_charge`)
- `ups_status{flag="..."}` for every status flag
- `device_info` from the `device.*` variables
- `error` when an endpoint cannot be collected
