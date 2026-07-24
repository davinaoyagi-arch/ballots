#!/usr/bin/env python3
"""Fetch, parse, validate, and publish Hawaiʻi absentee voting reports.

The normal mode discovers the newest complete set of five reports on the
Hawaiʻi Office of Elections website. A local mode is included so the parser can
be tested against downloaded county PDFs before the GitHub workflow is enabled.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import pdfplumber


REPORT_PAGE = "https://elections.hawaii.gov/resources/absentee-voting-report/"
REPORT_HOST = "elections.hawaii.gov"
REPORT_PATH_PREFIX = "/wp-content/uploads/"
HAWAII_TIME = timezone(timedelta(hours=-10), name="HST")
SCHEMA_VERSION = 1
EXPECTED_COUNTS = {
    "HAWAII": 42,
    "MAUI": 35,
    "KAUAI": 16,
    "OAHU": 154,
}
COUNTY_ORDER = tuple(EXPECTED_COUNTS)
REPORT_CODE_TO_COUNTY = {
    "STATE": "STATEWIDE",
    "01": "OAHU",
    "02": "HAWAII",
    "03": "KAUAI",
    "04": "MAUI",
}
COUNTY_TO_REPORT_CODE = {
    county: code for code, county in REPORT_CODE_TO_COUNTY.items() if county != "STATEWIDE"
}
PDF_NAME_PATTERN = re.compile(
    r"^AbsenteeRecon(?:(State)|DP-(01|02|03|04))-(\d{8})\.pdf$",
    re.IGNORECASE,
)
PRECINCT_PATTERN = re.compile(r"^\d{1,2}/\d{2}$")
TIMESTAMP_PATTERN = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s+(\d{1,2}:\d{2}:\d{2}\s+[AP]M)",
    re.IGNORECASE,
)
INTEGER_PATTERN = re.compile(r"^[\d,]+$")
BALLOT_FIELDS = (
    "electronicSent",
    "electronicReturned",
    "electronicInvalid",
    "earlyReturned",
    "mailSent",
    "mailReturned",
    "mailInvalid",
)


class ReportError(RuntimeError):
    """Raised when source discovery or validation fails."""


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self.text.append(stripped)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/ballot-returns.json"),
        help="Validated public ballot-return JSON.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/manifest.json"),
        help="Source URL and hash manifest.",
    )
    parser.add_argument(
        "--status",
        type=Path,
        default=Path("data/automation-status.json"),
        help="Low-frequency successful-check heartbeat.",
    )
    parser.add_argument(
        "--diagnostics-dir",
        type=Path,
        default=Path("tmp/refresh-diagnostics"),
        help="Files retained by GitHub Actions if parsing fails.",
    )
    parser.add_argument(
        "--local-pdf",
        action="append",
        default=[],
        metavar="COUNTY=PATH",
        help=(
            "Use local county PDFs instead of the network. Repeat for HAWAII, "
            "MAUI, KAUAI, and OAHU."
        ),
    )
    return parser.parse_args()


def clean_cell(value: str | None) -> str:
    return " ".join((value or "").replace("\u00a0", " ").split())


def parse_integer(value: str, context: str) -> int:
    cleaned = clean_cell(value).replace(",", "")
    if not cleaned.isdigit():
        raise ReportError(f"{context}: expected a nonnegative integer, got {value!r}")
    return int(cleaned)


def vector_from_record(record: dict[str, Any]) -> list[int]:
    returned = (
        record["electronicReturned"] + record["earlyReturned"] + record["mailReturned"]
    )
    sent = record["electronicSent"] + record["earlyReturned"] + record["mailSent"]
    return [
        record["electronicSent"],
        record["electronicReturned"],
        record["electronicInvalid"],
        record["earlyReturned"],
        record["mailSent"],
        record["mailReturned"],
        record["mailInvalid"],
        returned,
        sent,
    ]


def add_vectors(vectors: Iterable[list[int]]) -> list[int]:
    total = [0] * 9
    for vector in vectors:
        for index, value in enumerate(vector):
            total[index] += value
    return total


def parse_pdf_timestamp(text: str, label: str) -> datetime:
    match = TIMESTAMP_PATTERN.search(text)
    if not match:
        raise ReportError(f"{label}: report timestamp was not found")
    parsed = datetime.strptime(
        f"{match.group(1)} {match.group(2).upper()}", "%m/%d/%Y %I:%M:%S %p"
    )
    return parsed.replace(tzinfo=HAWAII_TIME)


def parse_county_pdf(path: Path, county: str) -> tuple[list[dict[str, Any]], list[int], datetime]:
    records: list[dict[str, Any]] = []
    summary_rows: list[list[int]] = []
    first_page_text = ""

    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                raise ReportError(f"{county}: PDF has no pages")
            first_page_text = pdf.pages[0].extract_text() or ""
            for page_number, page in enumerate(pdf.pages, start=1):
                for table in page.extract_tables():
                    for raw_row in table:
                        row = [clean_cell(cell) for cell in raw_row]
                        if len(row) != 10:
                            continue
                        if PRECINCT_PATTERN.fullmatch(row[0]):
                            values = [
                                parse_integer(value, f"{county} {row[0]} column {index + 2}")
                                for index, value in enumerate(row[1:])
                            ]
                            dp = row[0].replace("/", "-")
                            record = {
                                "county": county,
                                "dp": dp,
                                "electronicSent": values[0],
                                "electronicReturned": values[1],
                                "electronicInvalid": values[2],
                                "earlyReturned": values[3],
                                "mailSent": values[4],
                                "mailReturned": values[5],
                                "mailInvalid": values[6],
                            }
                            computed = vector_from_record(record)
                            if values[7] != computed[7]:
                                raise ReportError(
                                    f"{county} {dp}: VOTED is {values[7]}, expected {computed[7]}"
                                )
                            if values[8] != computed[8]:
                                raise ReportError(
                                    f"{county} {dp}: TOTAL is {values[8]}, expected {computed[8]}"
                                )
                            record.update(
                                {
                                    "returned": computed[7],
                                    "sent": computed[8],
                                    "invalid": values[2] + values[6],
                                    "rate": round(
                                        (computed[7] / computed[8]) * 100, 2
                                    )
                                    if computed[8]
                                    else 0.0,
                                }
                            )
                            records.append(record)
                        elif (
                            not row[0]
                            and all(INTEGER_PATTERN.fullmatch(value) for value in row[1:])
                        ):
                            summary_rows.append(
                                [
                                    parse_integer(
                                        value,
                                        f"{county} page {page_number} summary column {index + 2}",
                                    )
                                    for index, value in enumerate(row[1:])
                                ]
                            )
    except ReportError:
        raise
    except Exception as exc:
        raise ReportError(f"{county}: could not parse {path.name}: {exc}") from exc

    expected_count = EXPECTED_COUNTS[county]
    if len(records) != expected_count:
        raise ReportError(
            f"{county}: found {len(records)} precinct rows; expected {expected_count}"
        )
    keys = [record["dp"] for record in records]
    if len(keys) != len(set(keys)):
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        raise ReportError(f"{county}: duplicate precinct rows: {', '.join(duplicates)}")
    if len(summary_rows) != 1:
        raise ReportError(
            f"{county}: found {len(summary_rows)} county summary rows; expected 1"
        )

    calculated_summary = add_vectors(vector_from_record(record) for record in records)
    if calculated_summary != summary_rows[0]:
        raise ReportError(
            f"{county}: precinct sums {calculated_summary} do not match PDF summary "
            f"{summary_rows[0]}"
        )
    timestamp = parse_pdf_timestamp(first_page_text, county)
    return records, calculated_summary, timestamp


def normalized_county_label(value: str) -> str:
    return re.sub(r"[^a-z]", "", html.unescape(value).lower())


def parse_statewide_pdf(path: Path) -> tuple[dict[str, list[int]], list[int], datetime]:
    county_rows: dict[str, list[int]] = {}
    total_rows: list[list[int]] = []
    first_page_text = ""
    county_names = {
        "hawaii": "HAWAII",
        "maui": "MAUI",
        "kauai": "KAUAI",
        "honolulu": "OAHU",
        "cityandcountyofhonolulu": "OAHU",
    }

    try:
        with pdfplumber.open(path) as pdf:
            if not pdf.pages:
                raise ReportError("STATEWIDE: PDF has no pages")
            first_page_text = pdf.pages[0].extract_text() or ""
            for page in pdf.pages:
                for table in page.extract_tables():
                    for raw_row in table:
                        row = [clean_cell(cell) for cell in raw_row]
                        if len(row) != 10:
                            continue
                        label = normalized_county_label(row[0])
                        county = county_names.get(label)
                        if county and all(
                            INTEGER_PATTERN.fullmatch(value) for value in row[1:]
                        ):
                            if county in county_rows:
                                raise ReportError(
                                    f"STATEWIDE: duplicate {county} summary row"
                                )
                            county_rows[county] = [
                                parse_integer(value, f"STATEWIDE {county}")
                                for value in row[1:]
                            ]
                        elif (
                            not label
                            and all(INTEGER_PATTERN.fullmatch(value) for value in row[1:])
                        ):
                            total_rows.append(
                                [
                                    parse_integer(value, "STATEWIDE total")
                                    for value in row[1:]
                                ]
                            )
    except ReportError:
        raise
    except Exception as exc:
        raise ReportError(f"STATEWIDE: could not parse {path.name}: {exc}") from exc

    if set(county_rows) != set(COUNTY_ORDER):
        raise ReportError(
            "STATEWIDE: expected county rows for "
            f"{', '.join(COUNTY_ORDER)}; found {', '.join(sorted(county_rows))}"
        )
    if len(total_rows) != 1:
        raise ReportError(
            f"STATEWIDE: found {len(total_rows)} total rows; expected 1"
        )
    calculated = add_vectors(county_rows.values())
    if calculated != total_rows[0]:
        raise ReportError(
            f"STATEWIDE: county sums {calculated} do not match total {total_rows[0]}"
        )
    timestamp = parse_pdf_timestamp(first_page_text, "STATEWIDE")
    return county_rows, calculated, timestamp


def fetch_bytes(url: str, *, attempts: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "HawaiiBallotMapUpdater/1.0 (+https://github.com/davinaoyagi-arch/ballots)",
                "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status != 200:
                    raise ReportError(f"{url}: HTTP {response.status}")
                return response.read()
        except (OSError, urllib.error.URLError, ReportError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 2)
    raise ReportError(f"{url}: download failed after {attempts} attempts: {last_error}")


def discover_report_links(page_html: str) -> tuple[date, dict[str, str], date | None]:
    parser = PageParser()
    parser.feed(page_html)
    groups: dict[date, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))

    for href in parser.links:
        url = urllib.parse.urljoin(REPORT_PAGE, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() != "https" or parsed.hostname != REPORT_HOST:
            continue
        if not parsed.path.startswith(REPORT_PATH_PREFIX):
            continue
        filename = Path(urllib.parse.unquote(parsed.path)).name
        match = PDF_NAME_PATTERN.fullmatch(filename)
        if not match:
            continue
        report_date = datetime.strptime(match.group(3), "%Y%m%d").date()
        code = "STATE" if match.group(1) else match.group(2)
        kind = REPORT_CODE_TO_COUNTY[code.upper()]
        groups[report_date][kind].add(url)

    required = set(COUNTY_ORDER) | {"STATEWIDE"}
    complete_dates = [
        report_date
        for report_date, kinds in groups.items()
        if set(kinds) == required and all(len(urls) == 1 for urls in kinds.values())
    ]
    if not complete_dates:
        raise ReportError("No complete five-PDF report set was found on the official page")
    newest = max(complete_dates)
    links = {kind: next(iter(urls)) for kind, urls in groups[newest].items()}

    page_text = " ".join(parser.text)
    displayed_match = re.search(
        r"Last\s+Updated\s+on\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        page_text,
        re.IGNORECASE,
    )
    displayed_date = (
        datetime.strptime(displayed_match.group(1), "%B %d, %Y").date()
        if displayed_match
        else None
    )
    if displayed_date and displayed_date > newest:
        raise ReportError(
            f"Official page says {displayed_date}, but newest complete PDF set is {newest}; "
            "publication may still be in progress"
        )
    if displayed_date and displayed_date < newest:
        print(
            f"Warning: official page label is {displayed_date}, using complete PDF set {newest}",
            file=sys.stderr,
        )
    return newest, links, displayed_date


def reset_diagnostics_directory(path: Path) -> None:
    resolved = path.resolve()
    workspace = Path.cwd().resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ReportError("Diagnostics directory must be inside the working directory") from exc
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def download_live_reports(
    diagnostics_dir: Path,
) -> tuple[date, dict[str, Path], dict[str, dict[str, Any]], date | None]:
    cache_buster = int(time.time())
    page_url = f"{REPORT_PAGE}?_={cache_buster}"
    page_bytes = fetch_bytes(page_url)
    page_html = page_bytes.decode("utf-8", errors="replace")
    (diagnostics_dir / "source-page.html").write_text(page_html, encoding="utf-8")
    report_date, links, displayed_date = discover_report_links(page_html)

    paths: dict[str, Path] = {}
    sources: dict[str, dict[str, Any]] = {}
    for kind in ("STATEWIDE", *COUNTY_ORDER):
        url = links[kind]
        payload = fetch_bytes(url)
        if len(payload) < 20_000 or len(payload) > 20_000_000:
            raise ReportError(f"{kind}: unexpected PDF size of {len(payload)} bytes")
        if not payload.startswith(b"%PDF-"):
            raise ReportError(f"{kind}: downloaded file is not a PDF")
        filename = Path(urllib.parse.urlparse(url).path).name
        path = diagnostics_dir / filename
        path.write_bytes(payload)
        paths[kind] = path
        sources[kind] = {
            "url": url,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    return report_date, paths, sources, displayed_date


def parse_local_assignments(
    assignments: list[str], diagnostics_dir: Path
) -> tuple[date, dict[str, Path], dict[str, dict[str, Any]], None]:
    paths: dict[str, Path] = {}
    for assignment in assignments:
        if "=" not in assignment:
            raise ReportError(f"Invalid --local-pdf value: {assignment!r}")
        county, raw_path = assignment.split("=", 1)
        county = county.strip().upper()
        if county not in COUNTY_ORDER:
            raise ReportError(f"Unknown local county {county!r}")
        if county in paths:
            raise ReportError(f"Duplicate local PDF for {county}")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise ReportError(f"{county}: local PDF does not exist: {path}")
        paths[county] = path

    if set(paths) != set(COUNTY_ORDER):
        missing = sorted(set(COUNTY_ORDER) - set(paths))
        raise ReportError(f"Local mode is missing PDFs for: {', '.join(missing)}")

    timestamps = []
    for county, path in paths.items():
        with pdfplumber.open(path) as pdf:
            timestamps.append(parse_pdf_timestamp(pdf.pages[0].extract_text() or "", county))
    dates = {timestamp.date() for timestamp in timestamps}
    if len(dates) != 1:
        raise ReportError(f"Local PDFs have mixed report dates: {sorted(dates)}")
    report_date = next(iter(dates))
    date_token = report_date.strftime("%Y%m%d")
    sources: dict[str, dict[str, Any]] = {}
    for county, path in paths.items():
        payload = path.read_bytes()
        code = COUNTY_TO_REPORT_CODE[county]
        sources[county] = {
            "url": (
                f"https://{REPORT_HOST}{REPORT_PATH_PREFIX}"
                f"AbsenteeReconDP-{code}-{date_token}.pdf"
            ),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    return report_date, paths, sources, None


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"Could not read existing {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReportError(f"Existing {path} is not a JSON object")
    return value


def record_key(record: dict[str, Any]) -> str:
    return f"{record.get('county')}|{record.get('dp')}"


def validate_previous_publication(
    previous: dict[str, Any] | None,
    report_date: date,
    records: list[dict[str, Any]],
) -> None:
    if not previous:
        return
    meta = previous.get("meta")
    previous_records = previous.get("records")
    if not isinstance(meta, dict) or not isinstance(previous_records, list):
        raise ReportError("Existing public data has an unsupported structure")
    previous_date_text = meta.get("reportDate")
    try:
        previous_date = date.fromisoformat(str(previous_date_text))
    except ValueError as exc:
        raise ReportError("Existing public data has an invalid reportDate") from exc
    if report_date < previous_date:
        raise ReportError(
            f"Refusing to replace newer report {previous_date} with {report_date}"
        )
    old_keys = {record_key(record) for record in previous_records}
    new_keys = {record_key(record) for record in records}
    if old_keys != new_keys:
        missing = sorted(old_keys - new_keys)
        unexpected = sorted(new_keys - old_keys)
        raise ReportError(
            "Precinct keyset changed. "
            f"Missing: {missing[:8] or 'none'}; unexpected: {unexpected[:8] or 'none'}"
        )


def totals_object(summary: list[int], precincts: int) -> dict[str, int | float]:
    returned = summary[7]
    sent = summary[8]
    return {
        "precincts": precincts,
        "sent": sent,
        "returned": returned,
        "invalid": summary[2] + summary[6],
        "rate": round((returned / sent) * 100, 2) if sent else 0.0,
    }


def stable_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def atomic_write_if_changed(path: Path, content: str) -> bool:
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    if previous == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8", newline="\n")
    os.replace(temporary, path)
    return True


def build_publication(
    report_date: date,
    paths: dict[str, Path],
    sources: dict[str, dict[str, Any]],
    displayed_date: date | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    records: list[dict[str, Any]] = []
    county_summaries: dict[str, list[int]] = {}
    timestamps: list[datetime] = []

    for county in COUNTY_ORDER:
        county_records, summary, timestamp = parse_county_pdf(paths[county], county)
        records.extend(county_records)
        county_summaries[county] = summary
        timestamps.append(timestamp)

    if "STATEWIDE" in paths:
        statewide_rows, statewide_total, statewide_timestamp = parse_statewide_pdf(
            paths["STATEWIDE"]
        )
        timestamps.append(statewide_timestamp)
        for county in COUNTY_ORDER:
            if statewide_rows[county] != county_summaries[county]:
                raise ReportError(
                    f"{county}: county PDF summary {county_summaries[county]} does not "
                    f"match statewide PDF {statewide_rows[county]}"
                )
    else:
        statewide_total = add_vectors(county_summaries.values())

    timestamp_dates = {timestamp.date() for timestamp in timestamps}
    if timestamp_dates != {report_date}:
        raise ReportError(
            f"PDF timestamps {sorted(timestamp_dates)} do not match source date {report_date}"
        )
    report_timestamp = max(timestamps)
    records.sort(
        key=lambda record: (
            COUNTY_ORDER.index(record["county"]),
            tuple(int(part) for part in record["dp"].split("-")),
        )
    )

    if len(records) != sum(EXPECTED_COUNTS.values()):
        raise ReportError(f"Expected 247 records, found {len(records)}")
    keys = [record_key(record) for record in records]
    if len(keys) != len(set(keys)):
        raise ReportError("Duplicate county/precinct key found in combined records")

    totals = {
        "statewide": totals_object(statewide_total, len(records)),
        "counties": {
            county: totals_object(county_summaries[county], EXPECTED_COUNTS[county])
            for county in COUNTY_ORDER
        },
    }
    publication = {
        "schemaVersion": SCHEMA_VERSION,
        "meta": {
            "reportDate": report_date.isoformat(),
            "reportTimestamp": report_timestamp.isoformat(),
            "source": "Hawaiʻi Office of Elections absentee reconciliation reports",
            "officialReportPage": REPORT_PAGE,
            "totals": totals,
        },
        "records": records,
    }
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "reportDate": report_date.isoformat(),
        "reportTimestamp": report_timestamp.isoformat(),
        "officialPageDisplayedDate": displayed_date.isoformat() if displayed_date else None,
        "recordCount": len(records),
        "sourceFiles": {
            kind: sources[kind] for kind in ("STATEWIDE", *COUNTY_ORDER) if kind in sources
        },
    }
    return publication, manifest


def should_refresh_heartbeat(status: dict[str, Any] | None, today: date) -> bool:
    if not status:
        return True
    try:
        previous = date.fromisoformat(str(status["lastSuccessfulCheckDateHst"]))
    except (KeyError, ValueError):
        return True
    return (today - previous).days >= 30


def run(args: argparse.Namespace) -> int:
    reset_diagnostics_directory(args.diagnostics_dir)
    if args.local_pdf:
        report_date, paths, sources, displayed_date = parse_local_assignments(
            args.local_pdf, args.diagnostics_dir
        )
        mode = "local"
    else:
        report_date, paths, sources, displayed_date = download_live_reports(
            args.diagnostics_dir
        )
        mode = "network"

    publication, manifest = build_publication(
        report_date, paths, sources, displayed_date
    )
    previous_publication = load_json(args.output)
    validate_previous_publication(previous_publication, report_date, publication["records"])

    publication_text = stable_json(publication)
    manifest_text = stable_json(manifest)
    existing_publication_text = (
        args.output.read_text(encoding="utf-8") if args.output.is_file() else None
    )
    existing_manifest_text = (
        args.manifest.read_text(encoding="utf-8") if args.manifest.is_file() else None
    )
    data_changed = (
        publication_text != existing_publication_text
        or manifest_text != existing_manifest_text
    )

    previous_status = load_json(args.status)
    today_hst = datetime.now(HAWAII_TIME).date()
    heartbeat_due = should_refresh_heartbeat(previous_status, today_hst)
    status_changed = data_changed or heartbeat_due
    if status_changed:
        status = {
            "schemaVersion": SCHEMA_VERSION,
            "lastSuccessfulCheckDateHst": today_hst.isoformat(),
            "latestReportDate": report_date.isoformat(),
            "recordCount": len(publication["records"]),
            "mode": mode,
        }
        status_text = stable_json(status)
    else:
        status_text = args.status.read_text(encoding="utf-8")

    changed_paths = []
    if atomic_write_if_changed(args.output, publication_text):
        changed_paths.append(str(args.output))
    if atomic_write_if_changed(args.manifest, manifest_text):
        changed_paths.append(str(args.manifest))
    if atomic_write_if_changed(args.status, status_text):
        changed_paths.append(str(args.status))

    statewide = publication["meta"]["totals"]["statewide"]
    print(
        f"Validated {len(publication['records'])} precincts for {report_date}: "
        f"{statewide['returned']:,} returned of {statewide['sent']:,} sent."
    )
    if changed_paths:
        print("Updated: " + ", ".join(changed_paths))
    else:
        print("No published files changed.")
    return 0


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except Exception as exc:
        args.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        (args.diagnostics_dir / "error.txt").write_text(
            f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
        )
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
