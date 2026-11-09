"""Read log and trace coordinates from the pinned Eadro TrainTicket archive."""

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
from zipfile import ZipFile


CLOCK_SHIFT = 8 * 3600
LEVEL = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}\s+(INFO|WARN|ERROR|DEBUG|TRACE)\b")
TRACE_REF = re.compile(r"Span reported: ([0-9a-f]+):([0-9a-f]+):")


@dataclass(frozen=True)
class Span:
    trace: str
    span: str
    parent: str | None
    service: str
    start: float
    end: float
    duration: float
    is_server: bool
    error: bool


@dataclass(frozen=True)
class Log:
    time: float
    service: str
    index: int
    severity: str
    span_key: tuple[str, str] | None


@dataclass(frozen=True)
class Window:
    start: float
    end: float

    def contains(self, timestamp: float) -> bool:
        return self.start <= timestamp < self.end

    def contains_span(self, span: Span) -> bool:
        return self.contains(span.start) and span.end < self.end


def read_logs(archive: ZipFile, root: str) -> list[Log]:
    logs = []
    for service, lines in json.loads(archive.read(f"{root}/logs.json")).items():
        for index, line in enumerate(lines):
            level = LEVEL.match(line)
            if not level:
                continue
            seconds = datetime.fromisoformat(line[:23]).replace(tzinfo=UTC).timestamp()
            ref = TRACE_REF.search(line)
            logs.append(Log(seconds - CLOCK_SHIFT, service, index, level[1], ref.groups() if ref else None))
    return logs


def read_spans(archive: ZipFile, root: str, run: Window) -> dict[tuple[str, str], Span]:
    spans = {}
    for trace in json.loads(archive.read(f"{root}/spans.json")):
        for record in trace["spans"]:
            start = record["startTime"] / 1e6 - CLOCK_SHIFT
            if not run.contains(start):
                continue
            service = trace["processes"][record["processID"]]["serviceName"]
            tags = {tag["key"]: tag["value"] for tag in record["tags"]}
            duration = record["duration"] / 1e6
            trace_id = record["traceID"]
            parent = next(
                (ref["spanID"] for ref in record["references"]
                 if ref["refType"] == "CHILD_OF" and ref["traceID"] == trace_id),
                None,
            )
            span = Span(trace_id, record["spanID"], parent, service, start, start + duration, duration,
                        tags.get("span.kind") == "server",
                        tags.get("error") is True or int(tags.get("http.status_code", 0)) >= 500)
            key = (trace_id, span.span)
            if key in spans and spans[key] != span:
                raise ValueError(f"Conflicting duplicate span: {key}")
            spans[key] = span
    return spans
