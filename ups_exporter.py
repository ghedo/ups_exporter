#!/usr/bin/env python3
#
# Copyright (c) 2026, Alessandro Ghedini
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
# IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# Description: Prometheus textfile exporter for UPS data from one or
#             more NUT servers. The exporter runs the upsc client
#             against each server on every run, discovering the UPS
#             list and the variables it reports.
#
# The exported metric names, types and defaults match the
# nut_exporter (github.com/DRuggeri/nut_exporter). Because the exporter
# may be configured with several servers, every sample carries a
# `server` (host:port) and `ups` label.
#
# Endpoints use the `upsname@host[:port]` syntax (port defaults to
# 3493). If the server supports STARTTLS the connection is upgraded
# to TLS transparently by upsc; certificate verification is disabled,
# as upsc does by default.
#
# The metrics are written to a file in the Prometheus textfile format
# (see --metrics-file), to be read by the textfile collector of
# node_exporter. The exporter is meant to be run periodically, for
# example by prometheus-ups-exporter.timer.

import argparse
import os
import re
import subprocess
import sys
import tempfile
from prometheus_client import CollectorRegistry, Gauge, generate_latest

__version__ = '0.1.0'

DEFAULT_METRICS_FILE = '/var/lib/prometheus/ups.prom'
DEFAULT_PORT = 3493
DEFAULT_NAMESPACE = 'network_ups_tools'

# Variables exported by default, matching nut_exporter.
DEFAULT_VARIABLES = (
    'battery.charge,battery.voltage,battery.voltage.nominal,'
    'input.voltage,input.voltage.nominal,ups.load,ups.status'
)

# device.* variables that become device_info labels.
DEVICE_INFO_LABELS = (
    'model', 'mfr', 'serial', 'type', 'description',
    'contact', 'location', 'part', 'macaddr',
)

# Some drivers report only these legacy variables instead of the
# device.* equivalents, so they are used as a fallback. ups.type is
# deliberately not covered: it holds the topology, a different
# concept from device.type.
LEGACY_DEVICE_VARIABLES = {
    'ups.model': 'model',
    'ups.mfr': 'mfr',
    'ups.serial': 'serial',
}

# ups.status flags always exported, set to 0 when the server does not
# report them.
STATUS_FLAGS = (
    'OL', 'OB', 'LB', 'HB', 'RB', 'CHRG', 'DISCHRG',
    'BYPASS', 'CAL', 'OFF', 'OVER', 'TRIM', 'BOOST', 'FSD', 'SD'
)

# Value patterns used by go.nut to decide that a value is a number or
# a boolean.
NUMERIC_RE = re.compile(r'^-?[0-9.]+$')
ON_RE = re.compile('^(enable|enabled|on|true|active|activated)$',
                   re.IGNORECASE)
OFF_RE = re.compile('^(disable|disabled|off|false|inactive|deactivated)$',
                    re.IGNORECASE)


class NUTError(Exception):
    """Raised when an upsc invocation fails."""


def upsc(arguments, timeout):
    """Run upsc with `arguments` and return its decoded stdout.

    Raises:
        NUTError if upsc is missing, fails or times out.
    """
    try:
        result = subprocess.run(
            ['upsc', *arguments], capture_output=True, timeout=timeout
        )
    except FileNotFoundError:
        raise NUTError('upsc not found in PATH')
    except subprocess.TimeoutExpired:
        raise NUTError('upsc timed out after %d seconds' % timeout)

    if result.returncode != 0:
        # upsc prints connection noise to stderr. The error, if any,
        # is the last line.
        stderr = result.stderr.decode('utf-8', 'replace').strip()
        raise NUTError(stderr.rsplit('\n', 1)[-1] if stderr
                       else 'upsc failed')
    return result.stdout.decode('utf-8', 'replace')


def list_variables(ups, timeout):
    """Return (name, value) pairs for all variables of one UPS.

    upsc prints one `name: value` pair per line. Variable names never
    contain a colon, so the first ': ' separates the name from the
    value. The plain text output is used instead of the -j JSON flag,
    which only exists in NUT 2.8.5 and later.
    """
    variables = []
    for line in upsc([ups], timeout).splitlines():
        name, sep, value = line.partition(': ')
        if sep:
            variables.append((name, value.rstrip()))
    return variables


def convert_value(value):
    """Coerce a variable value like go.nut does.

    Returns an int, a float or the original string.
    """
    if value == 'enabled':
        return 1
    if value == 'disabled':
        return 0

    # NUMERIC_RE also admits non-numbers such as "-" and "1.2.3".
    if NUMERIC_RE.match(value):
        if value.count('.') == 0:
            try:
                return int(value)
            except ValueError:
                pass
        elif value.count('.') == 1:
            try:
                return float(value)
            except ValueError:
                pass

    return value


def log(message):
    print('ups_exporter: ' + message, file=sys.stderr)


def parse_endpoint(endpoint):
    """Parse an `upsname@host[:port]` endpoint.

    Returns:
        (str, str, int) UPS name, host, port.
    """
    upsname, _, hostport = endpoint.partition('@')
    if not upsname or not hostport or '@' in hostport:
        raise ValueError('expected upsname@host[:port]')

    host, _, port = hostport.partition(':')
    if not host or ':' in port:
        raise ValueError('expected upsname@host[:port]')
    if port:
        try:
            port_number = int(port)
        except ValueError:
            raise ValueError('invalid port: ' + port)
        if not 1 <= port_number <= 65535:
            raise ValueError('port out of range: ' + port)
    else:
        port_number = DEFAULT_PORT
    return upsname, host, port_number


def metric_name(namespace, variable):
    # Prometheus metric names allow only letters, digits, underscores
    # and colons.
    return namespace + '_' + variable.replace('.', '_').replace('-', '_')


def collect_endpoint(args, registry, gauges, upsname, host, port):
    """Add the metrics for one endpoint to the shared registry.

    `gauges` maps a variable name to the Gauge shared by all endpoints
    for that variable.

    Returns:
        (str or None) error message, or None on success
    """
    server = '%s:%d' % (host, port)
    try:
        variables = list_variables(
            '%s@%s' % (upsname, server), args.timeout
        )
        if not variables:
            return 'no variables reported by %s@%s' % (upsname, server)

        device = dict.fromkeys(DEVICE_INFO_LABELS, '')
        legacy = {}

        def gauge_for(variable, labelnames):
            # All endpoints share one Gauge per variable.
            gauge = gauges.get(variable)
            if gauge is None:
                gauge = Gauge(
                    metric_name(args.namespace, variable),
                    variable,
                    labelnames,
                    registry=registry,
                )
                gauges[variable] = gauge
            return gauge

        for variable, value in variables:
            # Collected even when filtered out, to keep device_info
            # complete.
            if variable.startswith('device.'):
                key = variable.split('.', 1)[1]
                if key in device:
                    device[key] = value
            else:
                label = LEGACY_DEVICE_VARIABLES.get(variable)
                if label is not None:
                    legacy[label] = value

            if args.variable_filter and variable not in args.variable_filter:
                continue

            if variable == 'ups.status':
                gauge = gauge_for(variable, ['server', 'ups', 'flag'])
                present = set(value.split())
                for flag in present:
                    gauge.labels(server=server, ups=upsname, flag=flag).set(1)

                # Standard flags not reported by the server are
                # forced to 0.
                for flag in STATUS_FLAGS:
                    if flag not in present:
                        gauge.labels(server=server, ups=upsname, flag=flag).set(0)
                continue

            number = convert_value(value)
            # Boolean strings are coerced here. Any other string is
            # not exported.
            if isinstance(number, str):
                if ON_RE.match(number):
                    number = 1
                elif OFF_RE.match(number):
                    number = 0
                else:
                    continue

            gauge_for(variable, ['server', 'ups']).labels(
                server=server, ups=upsname
            ).set(number)

        # Fall back to the legacy values for drivers that do not
        # report device.*.
        for label, value in legacy.items():
            if not device[label]:
                device[label] = value

        labels = dict(device)
        labels['server'] = server
        labels['ups'] = upsname

        gauge = gauges.get('device_info')
        if gauge is None:
            gauge = Gauge(
                args.namespace + '_device_info',
                'UPS Device information',
                sorted(labels),
                registry=registry,
            )
            gauges['device_info'] = gauge
        gauge.labels(**labels).set(1)

        return None
    except (OSError, ValueError, NUTError) as exc:
        return '%s@%s: %s' % (upsname, server, exc)


class SortedCollector(object):
    """Exposes the metric families of a registry in name order.

    prometheus_client returns them in registration order, which is
    not stable when variables appear or disappear.
    """

    def __init__(self, registry):
        self._registry = registry

    def collect(self):
        return sorted(self._registry.collect(), key=lambda metric: metric.name)


def collect_all(args):
    """Query every endpoint.

    Returns:
        (bytes, int) rendered metrics, number of failed endpoints
    """
    registry = CollectorRegistry()
    gauges = {}
    error_gauge = None
    errors = 0

    for upsname, host, port in args.endpoints:
        server = '%s:%d' % (host, port)
        error = collect_endpoint(
            args, registry, gauges, upsname, host, port
        )
        if error is not None:
            log(error)
            errors += 1

            # Created lazily so healthy runs emit no _error family.
            if error_gauge is None:
                error_gauge = Gauge(
                    args.namespace + '_error',
                    'Failure gathering UPS variables',
                    ['server', 'ups'],
                    registry=registry,
                )
            # NaN marks the failed endpoint without inventing a value.
            error_gauge.labels(
                server=server, ups=upsname
            ).set(float('nan'))

    return generate_latest(SortedCollector(registry)), errors


def write_metrics_file(path, data):
    """Atomically replace `path` with `data` (temp file, then rename).

    The temp file does not end in `.prom`, so a textfile collector
    scanning the directory never picks up a partial file.
    """
    directory = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(
        prefix='.ups.', suffix='.tmp', dir=directory
    )

    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)

        # mkstemp creates 0600 files. The collector usually runs as
        # another user.
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(
        description='Prometheus textfile exporter for NUT servers, '
        'using the upsc client.',
    )
    parser.add_argument(
        'endpoint',
        nargs='+',
        metavar='ENDPOINT',
        help='NUT server endpoint: upsname@host[:port] '
        '(port defaults to 3493).',
    )
    parser.add_argument(
        '--metrics-file', default=DEFAULT_METRICS_FILE,
        help='path of the Prometheus textfile to write '
        '(default: %(default)s)',
    )
    parser.add_argument(
        '--namespace', default=DEFAULT_NAMESPACE,
        help='metric namespace (default: %(default)s)',
    )
    parser.add_argument(
        '--variables', default=DEFAULT_VARIABLES,
        help='comma-separated list of NUT variables to export, an empty '
        'string exports every numeric variable (default: %(default)s)',
    )
    parser.add_argument(
        '--timeout', type=int, default=10,
        help='timeout in seconds for each upsc invocation '
        '(default: %(default)s)',
    )
    parser.add_argument(
        '--version', action='version',
        version='ups_exporter ' + __version__,
    )
    args = parser.parse_args()

    # An empty filter exports every variable.
    args.variable_filter = set()
    if args.variables:
        args.variable_filter = {
            variable for variable in args.variables.split(',') if variable
        }

    args.endpoints = []
    for endpoint in args.endpoint:
        try:
            args.endpoints.append(parse_endpoint(endpoint))
        except ValueError as exc:
            parser.error('invalid endpoint %s: %s' % (endpoint, exc))

    body, errors = collect_all(args)
    try:
        write_metrics_file(args.metrics_file, body)
    except OSError as exc:
        log('failed to write %s: %s' % (args.metrics_file, exc))
        sys.exit(2)

    log('wrote %d bytes to %s' % (len(body), args.metrics_file))
    # Exit 1 if any endpoint failed so the systemd run is marked
    # failed.
    sys.exit(1 if errors else 0)


if __name__ == '__main__':
    main()
