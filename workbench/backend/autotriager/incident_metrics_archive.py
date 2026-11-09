"""Read recorded samples scoped to an incident's captured evidence."""

import csv
from datetime import UTC, datetime
from functools import lru_cache
from hashlib import file_digest
import io
import json
import math
import re
from zipfile import BadZipFile, ZipFile

from .contracts import ROOT


# Same local, pinned source used to build the published TrainTicket cases.
ARCHIVE = '.local-proactive-eadro/train-ticket.zip'
ARCHIVE_MD5 = '2d77861e4a025781e598d51866fd6f4e'
METRIC_MEMBER = re.compile(
    r'Source member (TT Dataset/data/TT\.[A-Za-z0-9_-]+/metrics/([A-Za-z0-9_-]+)\.csv)\.$'
)
METRICS = {
    'cpu_usage_total': 'CPU usage',
    'memory_usage': 'Memory usage',
    'rx_bytes': 'Network received',
    'tx_bytes': 'Network sent',
}
MAX_CSV_BYTES = 2_000_000
MAX_POINTS = 160
REQUEST_JOURNALS = (
    '.local-live-alert-20261106/requests.jsonl',
    '.local-incident-lifecycle-20261106/requests.jsonl',
    '.local-incident-workbench-20261106-browser/live-session/requests.jsonl',
)
MAX_JOURNAL_SCAN_BYTES = 64 * 1024 * 1024
JOURNAL_WINDOW_SECONDS = 60
REQUEST_ID = re.compile(r'Request ([0-9a-f]{32}); HTTP [1-5][0-9]{2};')


@lru_cache(maxsize=4)
def _archive_matches(path, size, modified_ns):
    # Size and modification time invalidate this digest after a local replacement.
    with path.open('rb') as source:
        return file_digest(source, 'md5').hexdigest() == ARCHIVE_MD5


def _evidenced_members(case):
    candidates = {candidate['service'] for candidate in case['candidates']}
    members = {}
    for evidence in case['evidence']:
        if not re.fullmatch(re.escape(case['id']) + r'-[1-3]-metric', evidence['id']):
            continue
        match = METRIC_MEMBER.search(evidence['detail'])
        if match and match[2] in candidates:
            members[match[2]] = match[1]
    return members


def _read_series(archive, service, member, start, end):
    info = archive.getinfo(member)
    if info.file_size > MAX_CSV_BYTES:
        raise ValueError('TrainTicket metric member exceeds the local read limit')

    measurements = {}
    with archive.open(info) as raw, io.TextIOWrapper(raw, encoding='utf-8', newline='') as content:
        for row in csv.DictReader(content):
            timestamp = float(row['timestamp'])
            if start <= timestamp < end:
                if timestamp in measurements or len(measurements) >= MAX_POINTS:
                    raise ValueError('Unexpected TrainTicket metric sample density')
                measurements[timestamp] = row

    series = []
    for metric, title in METRICS.items():
        points = []
        for timestamp, row in sorted(measurements.items()):
            value = float(row.get(metric) or 'nan')
            if math.isfinite(value):
                points.append({'at': datetime.fromtimestamp(timestamp, UTC).isoformat(), 'value': value})
        if points:
            series.append({'id': f'{service}-{metric}', 'title': f'{service} · {title}',
                           'group': 'resources', 'unit': 'archive units',
                           'source': f'Eadro TrainTicket · {service}.csv · {metric}',
                           'scope': 'Raw service metric in the recorded fault window', 'points': points})
    return series


def archive_incident_metrics(case, root=ROOT):
    """Return samples scoped to services cited by the case and its UTC window."""
    result = {'incident_id': case['id'], 'start': case['start'], 'end': case['end'],
              'series': [], 'status': 'unavailable'}
    if not re.fullmatch(r'tt-\d{3}', case['id']):
        return result
    path = root / ARCHIVE
    try:
        identity = path.stat()
        if not _archive_matches(path, identity.st_size, identity.st_mtime_ns):
            return result
        start, end = (datetime.fromisoformat(case[key]).timestamp() for key in ('start', 'end'))
        with ZipFile(path) as archive:
            for service, member in _evidenced_members(case).items():
                result['series'].extend(_read_series(archive, service, member, start, end))
    except (BadZipFile, OSError, ValueError, KeyError, TypeError):
        pass
    if result['series']:
        result['status'] = 'partial'
    return result


def _finite_nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _journal_rows(path, service, observed):
    rows = []
    scanned = 0
    end = observed + JOURNAL_WINDOW_SECONDS
    with path.open('rb') as stream:
        for line in stream:
            scanned += len(line)
            if scanned > MAX_JOURNAL_SCAN_BYTES:
                return []
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict) or row.get('service') != service:
                continue
            timestamp = row.get('observed_at_unix')
            if not _finite_nonnegative(timestamp):
                continue
            if timestamp > end + 3:
                break
            if observed - JOURNAL_WINDOW_SECONDS <= timestamp <= end:
                rows.append(row)
    return rows


def recorded_live_incident_metrics(case, root=ROOT, request_log=None):
    """Recover checkout samples only from the journal containing the alert's request."""
    if case.get('source') != 'live':
        return []
    metric = case.get('metric_evidence') or {}
    request = case.get('matched_request') or {}
    request_id = request.get('request_id')
    if request_id is None:
        for evidence in case.get('evidence', []):
            if evidence['id'] == 'lab-request' and (match := REQUEST_ID.match(evidence['detail'])):
                request_id = match[1]
                break
    if not isinstance(request_id, str) or not re.fullmatch(r'[0-9a-f]{32}', request_id):
        return []
    try:
        captured_at = datetime.fromisoformat(metric.get('observed_at') or metric['end'])
        if captured_at.utcoffset() is None or not _finite_nonnegative(metric['value']):
            return []
        observed = captured_at.timestamp()
    except (KeyError, ValueError, TypeError, OverflowError):
        return []
    paths = [root / name for name in REQUEST_JOURNALS]
    if request_log is not None and request_log not in paths:
        paths.insert(0, request_log)
    for path in paths:
        try:
            rows = _journal_rows(path, metric['service'], observed)
        except (KeyError, OSError, OverflowError):
            continue
        anchors = [row for row in rows if row.get('request_id') == request_id
                   and 0 <= observed - row['observed_at_unix'] <= 3
                   and _finite_nonnegative(row.get('latency_ms'))
                   and math.isclose(row['latency_ms'], metric['value'], rel_tol=0, abs_tol=1e-8)]
        if len(anchors) == 1:
            return _request_series(rows, anchors[0], metric['threshold'])
    return []


def _request_series(rows, anchor, threshold):
    source = 'Recorded local request journal'
    scope = 'Observed requests from the same checkout route around the recorded alert'

    def lab_id(row):
        control = row.get('control')
        return control.get('lab_id') if isinstance(control, dict) else None

    anchor_lab = lab_id(anchor)
    route_rows = sorted((row for row in rows if row.get('route') == anchor.get('route')
                         and lab_id(row) == anchor_lab), key=lambda row: row['observed_at_unix'])

    def series(series_id, title, unit, values, **extra):
        return {'id': series_id, 'title': title, 'group': 'requests', 'unit': unit,
                'source': source, 'scope': scope,
                'points': [{'at': datetime.fromtimestamp(at, UTC).isoformat(), 'value': value}
                           for at, value in values], **extra}

    def latency_points(field, include):
        values = {row['observed_at_unix']: row[field] for row in route_rows
                  if include(row) and _finite_nonnegative(row.get(field))}
        at = sorted(values)
        if len(at) > MAX_POINTS:
            step = math.ceil(len(at) / (MAX_POINTS - 2))
            at = sorted({*at[::step], at[-1], anchor['observed_at_unix']})
        return [(stamp, values[stamp]) for stamp in at if stamp in values]

    result = [series('checkout_latency', 'Checkout request latency', 'ms',
                     latency_points('latency_ms', lambda row: True), threshold=threshold)]
    payment = latency_points('dependency_latency_ms', lambda row: row.get('dependency') == 'payment')
    if payment:
        result.append(series('payment_latency', 'Payment call latency', 'ms', payment))

    # Only report complete five-second intervals inside the observed journal range.
    first, last = route_rows[0]['observed_at_unix'], route_rows[-1]['observed_at_unix']
    buckets = {}
    for row in route_rows:
        at = 5 * math.floor(row['observed_at_unix'] / 5)
        if first <= at and at + 5 <= last:
            buckets.setdefault(at, []).append(row.get('http_status'))
    if buckets:
        result.append(series('journal_throughput', 'Recorded requests per second', 'req/s',
                             [(at + 5, len(statuses) / 5) for at, statuses in sorted(buckets.items())]))
        rates = [(at + 5, 100 * sum(status >= 500 for status in statuses) / len(statuses))
                 for at, statuses in sorted(buckets.items())
                 if all(type(status) is int and 100 <= status <= 599 for status in statuses)]
        if rates:
            result.append(series('journal_error_rate', 'Recorded HTTP 5xx rate', '%', rates))
    return result
