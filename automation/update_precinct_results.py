#!/usr/bin/env python3
"""Fetch, parse, validate, and publish Hawaii precinct election results.

The Office of Elections export is tab-delimited text with quoted fields. Some
quoted fields may contain physical line breaks, so this parser deliberately
uses Python's CSV implementation rather than line-oriented string splitting.
"""

from __future__ import annotations

import argparse
import calendar
import colorsys
import csv
import hashlib
import io
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
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


RESULTS_PAGE = "https://elections.hawaii.gov/election-results/"
RESULTS_HOST = "elections.hawaii.gov"
DEFAULT_ELECTION_LABEL = "2026 Primary"
DEFAULT_ELECTION_TITLE = "2026 Primary Election"
FINAL_PRECINCT_PDF_URL = (
    "https://elections.hawaii.gov/wp-content/results/2026%20Primary/precinct.pdf"
)
FINAL_PRECINCT_PDF_SHA256 = (
    "25ad6aeec27bb9be1d033e4f4876274da457e44bb422740325473f06cded0a08"
)
FINAL_PRECINCT_PDF_BYTES = 3_814_825
FINAL_REPORT_TIMESTAMP = "2026-08-14T18:36:38-10:00"
SCHEMA_VERSION = 1
EXPECTED_MAPPED_PRECINCTS = 247
EXPECTED_REPORTING_GROUPS = 249
EXPECTED_SPLITS = 496
EXPECTED_ROWS = 25_729
EXPECTED_RACES = 136
EXPECTED_CANDIDATES = 293
EXPECTED_UNMAPPED_GROUPS = {"OS I", "OS II"}
COUNTIES = {"HAWAII", "MAUI", "KAUAI", "OAHU"}
PARTY_LABELS = {
    "D": "Democratic",
    "G": "Green",
    "L": "Libertarian",
    "N": "Nonpartisan",
    "NON": "Nonpartisan",
    "R": "Republican",
}
COUNTY_LABELS = {
    "HAWAII": "Hawaiʻi County",
    "MAUI": "Maui County",
    "KAUAI": "Kauaʻi County",
    "OAHU": "Honolulu County",
}
LEVELS = (
    {"id": "federal", "label": "U.S. House"},
    {"id": "statewide", "label": "Statewide"},
    {"id": "state-senate", "label": "State Senate"},
    {"id": "state-house", "label": "State House"},
    {"id": "county", "label": "County"},
    {"id": "oha", "label": "Office of Hawaiian Affairs"},
)
HAWAII_TIME = timezone(timedelta(hours=-10), name="HST")
PRECINCT_PATTERN = re.compile(r"^(\d{1,2})-(\d{2})$")
EXCEL_DATE_PRECINCT_PATTERN = re.compile(r"^(\d{1,2})-([A-Za-z]{3})$")
MEDIA_PATH_PATTERN = re.compile(
    r"^/wp-content/results/([^/]+)/media\.txt$", re.IGNORECASE
)
SUMMARY_PATH_PATTERN = re.compile(
    r"^/wp-content/results/([^/]+)/summary\.txt$", re.IGNORECASE
)
REQUIRED_COLUMNS = (
    "Precinct_Name",
    "Split_Name",
    "precinct_splitId",
    "Reg_voters",
    "Ballots",
    "Reporting",
    "Contest_id",
    "Contest_title",
    "Contest_party",
    "Choice_id",
    "Candidate_name",
    "Choice_party",
    "Candidate_Type",
    "Mail votes",
    "In-Person votes",
)
SUMMARY_REQUIRED_COLUMNS = (
    "Contest ID",
    "Contest Title",
    "Contest Party",
    "Registered Voters",
    "Total Precincts",
    "Counted Precincts",
    "Candidate ID",
    "Candidate Name",
    "Candidate Party",
    "Mail Votes",
    "In-Person Votes",
    "Total Votes",
)


class ResultsError(RuntimeError):
    """Raised when discovery, parsing, or validation fails."""


class ResultsPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


@dataclass(frozen=True)
class SplitRecord:
    raw_precinct: str
    group_key: str
    split_name: str
    split_id: str
    registered_voters: int
    ballots: int
    reporting: int


@dataclass
class ParsedExport:
    rows: int
    races: dict[str, dict[str, Any]]
    splits: dict[str, SplitRecord]
    group_splits: dict[str, set[str]]
    race_group_votes: dict[str, dict[str, dict[str, int]]]
    race_group_splits: dict[str, dict[str, set[str]]]


@dataclass
class ParsedSummary:
    rows: int
    races: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class LoadedSources:
    precinct: bytes
    precinct_url: str
    precinct_timestamp: datetime
    summary: bytes | None
    summary_url: str | None
    summary_timestamp: datetime | None
    mode: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/precinct-results.json"),
        help="Validated public precinct-results JSON.",
    )
    parser.add_argument(
        "--public-output",
        type=Path,
        default=Path("public/data/precinct-results.json"),
        help="Embedded fallback copy served with the map.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("data/precinct-results-manifest.json"),
        help="Source URL and hash manifest.",
    )
    parser.add_argument(
        "--status",
        type=Path,
        default=Path("data/precinct-results-status.json"),
        help="Low-frequency successful-check heartbeat.",
    )
    parser.add_argument(
        "--precinct-registry",
        type=Path,
        default=Path("data/ballot-returns.json"),
        help="Existing validated 247-precinct feed used for county/key joins.",
    )
    parser.add_argument(
        "--diagnostics-dir",
        type=Path,
        default=Path("tmp/precinct-results-diagnostics"),
        help="Files retained by GitHub Actions if parsing fails.",
    )
    parser.add_argument(
        "--local-file",
        type=Path,
        help="Parse a downloaded Precinct.txt instead of the network source.",
    )
    parser.add_argument(
        "--local-summary-file",
        type=Path,
        help="Optional statewide summary text file to merge with a local precinct file.",
    )
    parser.add_argument(
        "--source-url",
        help="Use this source URL instead of discovering media.txt on the results page.",
    )
    parser.add_argument(
        "--summary-source-url",
        help="Use this statewide summary URL instead of discovering summary.txt.",
    )
    parser.add_argument(
        "--report-timestamp",
        help="ISO timestamp override for a local file.",
    )
    parser.add_argument(
        "--election-label",
        default=DEFAULT_ELECTION_LABEL,
        help="Official results-path label used to select the current media.txt link.",
    )
    parser.add_argument(
        "--election-title",
        default=DEFAULT_ELECTION_TITLE,
        help="Human-readable election title stored in the public feed.",
    )
    return parser.parse_args()


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_header(value: str) -> str:
    return value.strip().lstrip("#").strip().strip('"')


def normalize_precinct_name(value: str) -> str:
    stripped = normalize_whitespace(value)
    match = PRECINCT_PATTERN.fullmatch(stripped)
    if match:
        return f"{int(match.group(1)):02d}-{int(match.group(2)):02d}"

    date_match = EXCEL_DATE_PRECINCT_PATTERN.fullmatch(stripped)
    if date_match:
        month_numbers = {
            name.lower(): index
            for index, name in enumerate(calendar.month_abbr)
            if name
        }
        month = month_numbers.get(date_match.group(2).lower())
        if month is None:
            raise ResultsError(f"Unknown month abbreviation in precinct {value!r}")
        return f"{month:02d}-{int(date_match.group(1)):02d}"

    if re.fullmatch(r"OS\s+[IVX]+", stripped, re.IGNORECASE):
        return stripped.upper()
    raise ResultsError(f"Unsupported precinct label {value!r}")


def load_precinct_registry(path: Path) -> dict[str, str]:
    """Load the established map keyset and its county for strict joins."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResultsError(f"Could not read precinct registry {path}: {exc}") from exc

    raw_records: list[dict[str, Any]]
    if isinstance(document, dict) and isinstance(document.get("records"), list):
        raw_records = document["records"]
    elif isinstance(document, dict) and isinstance(document.get("features"), list):
        raw_records = [
            feature.get("properties", {})
            for feature in document["features"]
            if isinstance(feature, dict)
        ]
    else:
        raise ResultsError(
            f"Precinct registry {path} must contain records or GeoJSON features"
        )

    registry: dict[str, str] = {}
    for index, raw in enumerate(raw_records, start=1):
        if not isinstance(raw, dict):
            raise ResultsError(f"Precinct registry row {index} is not an object")
        dp = normalize_precinct_name(str(raw.get("dp", "")))
        county = normalize_whitespace(str(raw.get("county", ""))).upper()
        if county not in COUNTIES:
            raise ResultsError(
                f"Precinct registry row {index} has unsupported county {county!r}"
            )
        previous = registry.get(dp)
        if previous is not None and previous != county:
            raise ResultsError(f"Precinct {dp} appears in conflicting counties")
        registry[dp] = county

    if len(registry) != EXPECTED_MAPPED_PRECINCTS:
        raise ResultsError(
            f"Precinct registry has {len(registry)} unique keys; "
            f"expected {EXPECTED_MAPPED_PRECINCTS}"
        )
    return registry


def roman_to_number(value: str) -> str:
    values = {"I": 1, "V": 5, "X": 10}
    total = 0
    previous = 0
    for character in reversed(value.upper()):
        current = values.get(character)
        if current is None:
            raise ResultsError(f"Unsupported Roman district number {value!r}")
        total += -current if current < previous else current
        previous = max(previous, current)
    return str(total)


def classify_race(
    title: str,
    party: str,
    mapped_groups: Iterable[str],
    precinct_registry: dict[str, str] | None,
) -> dict[str, str | None]:
    """Classify every official contest into the map's navigation levels."""
    county: str | None = None
    district: str | None = None
    if title.startswith("U.S. Representative, Dist "):
        level, category = "federal", "us-house"
        match = re.search(r"Dist ([IVX]+)$", title)
        if not match:
            raise ResultsError(f"Could not parse congressional district from {title!r}")
        district = roman_to_number(match.group(1))
    elif title == "Governor":
        level, category = "statewide", "governor"
    elif title == "Lieutenant Governor":
        level, category = "statewide", "lieutenant-governor"
    elif title.startswith("State Senator, Dist "):
        level, category = "state-senate", "state-senate"
        match = re.search(r"Dist (\d+)", title)
        if not match:
            raise ResultsError(f"Could not parse senate district from {title!r}")
        district = match.group(1)
    elif title.startswith("State Representative, Dist "):
        level, category = "state-house", "state-house"
        match = re.search(r"Dist (\d+)", title)
        if not match:
            raise ResultsError(f"Could not parse house district from {title!r}")
        district = match.group(1)
    elif title == "At-Large Trustee":
        level, category, district = "oha", "oha-trustee", "At-Large"
    elif title == "Mayor" or title.startswith("Councilmember"):
        level = "county"
        category = "mayor" if title == "Mayor" else "county-council"
        if precinct_registry is not None:
            counties = {precinct_registry[group] for group in mapped_groups}
            if len(counties) != 1:
                raise ResultsError(
                    f"County contest {title!r} maps to {sorted(counties)}, "
                    "expected one county"
                )
            county = next(iter(counties))
        district_match = re.search(r"Dist ([IVX]+|\d+)", title)
        place_match = re.search(r"\(([^)]+)\)", title)
        if district_match:
            raw_district = district_match.group(1)
            district = (
                roman_to_number(raw_district)
                if re.fullmatch(r"[IVX]+", raw_district)
                else str(int(raw_district))
            )
        elif place_match:
            district = place_match.group(1)
        elif title == "Councilmember":
            district = "At-Large"
    else:
        raise ResultsError(f"Unclassified contest title {title!r}")

    party_label = PARTY_LABELS.get(party)
    if party_label is None:
        raise ResultsError(f"Unsupported contest party {party!r} for {title!r}")
    if county is not None:
        display_title = f"{title} — {COUNTY_LABELS[county]}"
    elif party not in {"NON"}:
        display_title = f"{title} — {party_label}"
    else:
        display_title = title
    return {
        "displayTitle": display_title,
        "level": level,
        "category": category,
        "partyLabel": party_label,
        "county": county,
        "district": district,
    }


def parse_integer(value: str, context: str) -> int:
    text = value.strip().replace(",", "")
    if not re.fullmatch(r"\d+", text):
        raise ResultsError(f"{context}: expected a nonnegative integer, got {value!r}")
    return int(text)


def choice_sort_key(value: str) -> tuple[int, int | str]:
    return (0, int(value)) if value.isdigit() else (1, value.casefold())


def race_sort_key(race: dict[str, Any]) -> tuple[str, str, tuple[int, int | str]]:
    return (
        str(race["title"]).casefold(),
        str(race["party"]).casefold(),
        choice_sort_key(str(race["contestId"])),
    )


def deterministic_candidate_color(contest_id: str, choice_id: str) -> str:
    contest_seed = int(hashlib.sha256(contest_id.encode("utf-8")).hexdigest()[:8], 16)
    if choice_id.isdigit():
        choice_seed = max(0, int(choice_id) - 1)
    else:
        choice_seed = int(
            hashlib.sha256(choice_id.encode("utf-8")).hexdigest()[:8], 16
        )
    hue = (contest_seed % 360 + choice_seed * 137.508) % 360
    saturation = 0.68 + (choice_seed % 3) * 0.04
    lightness = 0.43 + (choice_seed % 2) * 0.09
    red, green, blue = colorsys.hls_to_rgb(hue / 360, lightness, saturation)
    return f"#{round(red * 255):02x}{round(green * 255):02x}{round(blue * 255):02x}"


def decode_export(source: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return source.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ResultsError("Precinct export is not valid UTF-8 or Windows-1252 text")


def is_spreadsheet_column_row(values: list[str]) -> bool:
    return bool(values) and values == [
        f"Column{index}" for index in range(1, len(values) + 1)
    ]


def parse_precinct_export(source: bytes) -> ParsedExport:
    text = decode_export(source)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t")
    try:
        first_row = next(reader)
        preamble = next(reader) if is_spreadsheet_column_row(first_row) else first_row
        raw_headers = next(reader)
    except StopIteration as exc:
        raise ResultsError("Precinct export is missing its preamble or header") from exc

    if not preamble or normalize_whitespace(preamble[0]) != "#FormatVersion 1":
        raise ResultsError("Precinct export has an unsupported format version")
    headers = [normalize_header(value) for value in raw_headers]
    missing = [column for column in REQUIRED_COLUMNS if column not in headers]
    if missing:
        raise ResultsError(f"Precinct export is missing columns: {', '.join(missing)}")

    races: dict[str, dict[str, Any]] = {}
    splits: dict[str, SplitRecord] = {}
    group_splits: dict[str, set[str]] = defaultdict(set)
    race_group_votes: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    race_group_splits: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    logical_keys: set[tuple[str, str, str]] = set()
    row_count = 0

    for row_number, values in enumerate(reader, start=3):
        if not values or all(not value.strip() for value in values):
            continue
        if len(values) != len(headers):
            raise ResultsError(
                f"Row {row_number}: found {len(values)} columns; expected {len(headers)}"
            )
        row_count += 1
        row = dict(zip(headers, values, strict=True))
        raw_precinct = normalize_whitespace(row["Precinct_Name"])
        group_key = normalize_precinct_name(raw_precinct)
        split_id = normalize_whitespace(row["precinct_splitId"])
        if not split_id:
            raise ResultsError(f"Row {row_number}: precinct_splitId is blank")

        split = SplitRecord(
            raw_precinct=raw_precinct,
            group_key=group_key,
            split_name=normalize_whitespace(row["Split_Name"]).upper(),
            split_id=split_id,
            registered_voters=parse_integer(
                row["Reg_voters"], f"Row {row_number} Reg_voters"
            ),
            ballots=parse_integer(row["Ballots"], f"Row {row_number} Ballots"),
            reporting=parse_integer(row["Reporting"], f"Row {row_number} Reporting"),
        )
        previous_split = splits.get(split_id)
        if previous_split is not None and previous_split != split:
            raise ResultsError(
                f"Split {split_id} has conflicting precinct, turnout, or reporting values"
            )
        splits[split_id] = split
        group_splits[group_key].add(split_id)

        contest_id = normalize_whitespace(row["Contest_id"])
        choice_id = normalize_whitespace(row["Choice_id"])
        if not contest_id or not choice_id:
            raise ResultsError(f"Row {row_number}: contest or choice ID is blank")
        logical_key = (split_id, contest_id, choice_id)
        if logical_key in logical_keys:
            raise ResultsError(
                f"Row {row_number}: duplicate split/contest/choice record {logical_key}"
            )
        logical_keys.add(logical_key)

        title = normalize_whitespace(row["Contest_title"])
        party = normalize_whitespace(row["Contest_party"])
        race = races.setdefault(
            contest_id,
            {
                "contestId": contest_id,
                "title": title,
                "party": party,
                "choices": {},
            },
        )
        if race["title"] != title or race["party"] != party:
            raise ResultsError(f"Contest {contest_id} has conflicting title or party")

        choice = {
            "choiceId": choice_id,
            "name": normalize_whitespace(row["Candidate_name"]),
            "party": normalize_whitespace(row["Choice_party"]),
            "type": normalize_whitespace(row["Candidate_Type"]),
        }
        previous_choice = race["choices"].get(choice_id)
        if previous_choice is not None and previous_choice != choice:
            raise ResultsError(
                f"Contest {contest_id} choice {choice_id} has conflicting metadata"
            )
        race["choices"][choice_id] = choice

        votes = parse_integer(row["Mail votes"], f"Row {row_number} Mail votes")
        votes += parse_integer(
            row["In-Person votes"], f"Row {row_number} In-Person votes"
        )
        race_group_votes[contest_id][group_key][choice_id] += votes
        race_group_splits[contest_id][group_key].add(split_id)

    if row_count == 0 or not races or not splits:
        raise ResultsError("Precinct export contains no result records")

    return ParsedExport(
        rows=row_count,
        races=races,
        splits=splits,
        group_splits=dict(group_splits),
        race_group_votes={
            contest_id: {
                group: dict(choice_votes) for group, choice_votes in groups.items()
            }
            for contest_id, groups in race_group_votes.items()
        },
        race_group_splits={
            contest_id: {group: set(split_ids) for group, split_ids in groups.items()}
            for contest_id, groups in race_group_splits.items()
        },
    )


def parse_summary_export(source: bytes) -> ParsedSummary:
    text = decode_export(source)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="\t")
    try:
        preamble = next(reader)
        raw_headers = next(reader)
    except StopIteration as exc:
        raise ResultsError("Summary export is missing its preamble or header") from exc

    if not preamble or normalize_whitespace(preamble[0]) != "#FormatVersion 1":
        raise ResultsError("Summary export has an unsupported format version")
    headers = [normalize_header(value) for value in raw_headers]
    missing = [column for column in SUMMARY_REQUIRED_COLUMNS if column not in headers]
    if missing:
        raise ResultsError(f"Summary export is missing columns: {', '.join(missing)}")

    races: dict[str, dict[str, Any]] = {}
    logical_keys: set[tuple[str, str]] = set()
    row_count = 0
    for row_number, values in enumerate(reader, start=3):
        if not values or all(not value.strip() for value in values):
            continue
        if len(values) != len(headers):
            raise ResultsError(
                f"Summary row {row_number}: found {len(values)} columns; "
                f"expected {len(headers)}"
            )
        row_count += 1
        row = dict(zip(headers, values, strict=True))
        contest_id = normalize_whitespace(row["Contest ID"])
        choice_id = normalize_whitespace(row["Candidate ID"])
        if not contest_id or not choice_id:
            raise ResultsError(
                f"Summary row {row_number}: contest or candidate ID is blank"
            )
        logical_key = (contest_id, choice_id)
        if logical_key in logical_keys:
            raise ResultsError(
                f"Summary row {row_number}: duplicate contest/candidate record "
                f"{logical_key}"
            )
        logical_keys.add(logical_key)

        title = normalize_whitespace(row["Contest Title"])
        party = normalize_whitespace(row["Contest Party"])
        total_precincts = parse_integer(
            row["Total Precincts"], f"Summary row {row_number} Total Precincts"
        )
        counted_precincts = parse_integer(
            row["Counted Precincts"], f"Summary row {row_number} Counted Precincts"
        )
        registered_voters = parse_integer(
            row["Registered Voters"],
            f"Summary row {row_number} Registered Voters",
        )
        race = races.setdefault(
            contest_id,
            {
                "contestId": contest_id,
                "title": title,
                "party": party,
                "registeredVoters": registered_voters,
                "totalPrecincts": total_precincts,
                "countedPrecincts": counted_precincts,
                "choices": {},
            },
        )
        expected_metadata = (
            title,
            party,
            registered_voters,
            total_precincts,
            counted_precincts,
        )
        actual_metadata = (
            race["title"],
            race["party"],
            race["registeredVoters"],
            race["totalPrecincts"],
            race["countedPrecincts"],
        )
        if actual_metadata != expected_metadata:
            raise ResultsError(
                f"Summary contest {contest_id} has conflicting race metadata"
            )

        mail_votes = parse_integer(
            row["Mail Votes"], f"Summary row {row_number} Mail Votes"
        )
        in_person_votes = parse_integer(
            row["In-Person Votes"],
            f"Summary row {row_number} In-Person Votes",
        )
        total_votes = parse_integer(
            row["Total Votes"], f"Summary row {row_number} Total Votes"
        )
        if total_votes != mail_votes + in_person_votes:
            raise ResultsError(
                f"Summary row {row_number}: Total Votes does not equal mail plus "
                "in-person votes"
            )
        race["choices"][choice_id] = {
            "choiceId": choice_id,
            "name": normalize_whitespace(row["Candidate Name"]),
            "party": normalize_whitespace(row["Candidate Party"]),
            "mailVotes": mail_votes,
            "inPersonVotes": in_person_votes,
            "votes": mail_votes + in_person_votes,
        }

    if row_count == 0 or not races:
        raise ResultsError("Summary export contains no result records")
    return ParsedSummary(rows=row_count, races=races)


def is_mapped_precinct(group_key: str) -> bool:
    return bool(re.fullmatch(r"\d{2}-\d{2}", group_key))


def validate_summary_export(parsed: ParsedExport, summary: ParsedSummary) -> None:
    precinct_contests = set(parsed.races)
    summary_contests = set(summary.races)
    if precinct_contests != summary_contests:
        missing = sorted(precinct_contests - summary_contests, key=choice_sort_key)
        extra = sorted(summary_contests - precinct_contests, key=choice_sort_key)
        raise ResultsError(
            "Summary contest keyset does not match precinct detail "
            f"(missing={missing}, extra={extra})"
        )

    for contest_id, precinct_race in parsed.races.items():
        summary_race = summary.races[contest_id]
        if (
            precinct_race["title"] != summary_race["title"]
            or precinct_race["party"] != summary_race["party"]
        ):
            raise ResultsError(
                f"Summary contest {contest_id} title or party does not match precinct detail"
            )
        precinct_choices = set(precinct_race["choices"])
        summary_choices = set(summary_race["choices"])
        if precinct_choices != summary_choices:
            raise ResultsError(
                f"Summary choices for contest {contest_id} do not match precinct detail"
            )
        for choice_id, precinct_choice in precinct_race["choices"].items():
            summary_choice = summary_race["choices"][choice_id]
            if precinct_choice["name"] != summary_choice["name"]:
                raise ResultsError(
                    f"Summary contest {contest_id} choice {choice_id} name does not "
                    "match precinct detail"
                )
            precinct_total = sum(
                group_votes.get(choice_id, 0)
                for group_votes in parsed.race_group_votes[contest_id].values()
            )
            if precinct_total != summary_choice["votes"]:
                raise ResultsError(
                    f"Summary contest {contest_id} choice {choice_id} reports "
                    f"{summary_choice['votes']} votes; precinct detail totals "
                    f"{precinct_total}"
                )


def turnout_row(parsed: ParsedExport, group_key: str) -> dict[str, Any]:
    split_rows = [parsed.splits[split_id] for split_id in parsed.group_splits[group_key]]
    registered_voters = max(row.registered_voters for row in split_rows)
    ballots = sum(row.ballots for row in split_rows)
    reporting_splits = sum(1 for row in split_rows if row.reporting > 0)
    return {
        "dp" if is_mapped_precinct(group_key) else "group": group_key,
        "registeredVoters": registered_voters,
        "ballots": ballots,
        "reportingSplits": reporting_splits,
        "totalSplits": len(split_rows),
        "turnoutRate": round((ballots / registered_voters) * 100, 2)
        if registered_voters
        else 0.0,
    }


def result_group_row(
    parsed: ParsedExport,
    contest_id: str,
    group_key: str,
    choice_ids: list[str],
) -> dict[str, Any]:
    votes_by_choice = parsed.race_group_votes[contest_id][group_key]
    votes = [votes_by_choice.get(choice_id, 0) for choice_id in choice_ids]
    total_votes = sum(votes)
    leaders: list[str] = []
    if total_votes:
        leader_votes = max(votes)
        leaders = [
            choice_id
            for choice_id, choice_votes in zip(choice_ids, votes, strict=True)
            if choice_votes == leader_votes
        ]
    split_ids = parsed.race_group_splits[contest_id][group_key]
    reporting_splits = sum(
        1 for split_id in split_ids if parsed.splits[split_id].reporting > 0
    )
    return {
        "dp" if is_mapped_precinct(group_key) else "group": group_key,
        "votes": votes,
        "totalVotes": total_votes,
        "leaderChoiceIds": leaders,
        "reportingSplits": reporting_splits,
        "totalSplits": len(split_ids),
    }


def build_publication(
    parsed: ParsedExport,
    *,
    summary: ParsedSummary | None = None,
    election_title: str,
    report_timestamp: str,
    source_url: str,
    source_sha256: str,
    source_bytes: int,
    summary_source_url: str | None = None,
    summary_source_sha256: str | None = None,
    summary_source_bytes: int | None = None,
    pdf_source_url: str | None = None,
    pdf_source_sha256: str | None = None,
    pdf_source_bytes: int | None = None,
    report_status: str = "preliminary",
    precinct_registry: dict[str, str] | None = None,
    expected_mapped_precincts: int | None = EXPECTED_MAPPED_PRECINCTS,
    strict_current_report: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if summary is not None:
        validate_summary_export(parsed, summary)
    mapped_groups = sorted(
        (group for group in parsed.group_splits if is_mapped_precinct(group)),
        key=lambda value: tuple(int(part) for part in value.split("-")),
    )
    unmapped_groups = sorted(
        group for group in parsed.group_splits if not is_mapped_precinct(group)
    )
    if expected_mapped_precincts is not None and len(mapped_groups) != expected_mapped_precincts:
        raise ResultsError(
            f"Found {len(mapped_groups)} mapped precincts; expected {expected_mapped_precincts}"
        )
    if precinct_registry is not None and set(mapped_groups) != set(precinct_registry):
        missing = sorted(set(precinct_registry) - set(mapped_groups))
        extra = sorted(set(mapped_groups) - set(precinct_registry))
        raise ResultsError(
            "Precinct detail keyset does not match the established map "
            f"(missing={missing}, extra={extra})"
        )
    if strict_current_report:
        if summary is None:
            raise ResultsError("The current report requires the statewide summary export")
        checks = {
            "precinct rows": (parsed.rows, EXPECTED_ROWS),
            "summary rows": (summary.rows, EXPECTED_CANDIDATES),
            "contests": (len(parsed.races), EXPECTED_RACES),
            "reporting groups": (len(parsed.group_splits), EXPECTED_REPORTING_GROUPS),
            "splits": (len(parsed.splits), EXPECTED_SPLITS),
        }
        for label, (actual, expected) in checks.items():
            if actual != expected:
                raise ResultsError(
                    f"Current report has {actual} {label}; expected {expected}"
                )
        if set(unmapped_groups) != EXPECTED_UNMAPPED_GROUPS:
            raise ResultsError(
                f"Unmapped groups are {unmapped_groups}; expected "
                f"{sorted(EXPECTED_UNMAPPED_GROUPS)}"
            )

    turnout_precincts = [turnout_row(parsed, group) for group in mapped_groups]
    if precinct_registry is not None:
        for row in turnout_precincts:
            row["county"] = precinct_registry[row["dp"]]
    turnout_unmapped = [turnout_row(parsed, group) for group in unmapped_groups]
    races: list[dict[str, Any]] = []
    candidate_count = 0

    for contest_id, race_source in parsed.races.items():
        choice_ids = sorted(race_source["choices"], key=choice_sort_key)
        candidate_count += len(choice_ids)
        group_votes = parsed.race_group_votes[contest_id]
        precinct_candidate_totals = {
            choice_id: sum(
                votes_by_choice.get(choice_id, 0)
                for votes_by_choice in group_votes.values()
            )
            for choice_id in choice_ids
        }
        summary_race = summary.races[contest_id] if summary is not None else None
        candidate_totals = (
            {
                choice_id: summary_race["choices"][choice_id]["votes"]
                for choice_id in choice_ids
            }
            if summary_race is not None
            else precinct_candidate_totals
        )
        total_votes = sum(candidate_totals.values())
        used_colors: set[str] = set()
        candidates: list[dict[str, Any]] = []
        for choice_id in choice_ids:
            choice = race_source["choices"][choice_id]
            color = deterministic_candidate_color(contest_id, choice_id)
            if color in used_colors:
                raise ResultsError(
                    f"Contest {contest_id} generated duplicate candidate color {color}"
                )
            used_colors.add(color)
            votes = candidate_totals[choice_id]
            candidates.append(
                {
                    **choice,
                    "color": color,
                    "votes": votes,
                    "percentage": round((votes / total_votes) * 100, 2)
                    if total_votes
                    else 0.0,
                }
            )

        mapped_result_groups = sorted(
            (group for group in group_votes if is_mapped_precinct(group)),
            key=lambda value: tuple(int(part) for part in value.split("-")),
        )
        unmapped_result_groups = sorted(
            group for group in group_votes if not is_mapped_precinct(group)
        )
        precincts = [
            result_group_row(parsed, contest_id, group, choice_ids)
            for group in mapped_result_groups
        ]
        if precinct_registry is not None:
            for row in precincts:
                row["county"] = precinct_registry[row["dp"]]
        other_groups = [
            result_group_row(parsed, contest_id, group, choice_ids)
            for group in unmapped_result_groups
        ]
        all_groups = [*precincts, *other_groups]
        classification = classify_race(
            race_source["title"],
            race_source["party"],
            mapped_result_groups,
            precinct_registry,
        )
        races.append(
            {
                "contestId": contest_id,
                "title": race_source["title"],
                "party": race_source["party"],
                **classification,
                "totalVotes": total_votes,
                "overallTotalsSource": (
                    "statewide-summary" if summary_race is not None else "precinct-detail"
                ),
                "officialTotalPrecincts": (
                    summary_race["totalPrecincts"] if summary_race is not None else None
                ),
                "officialCountedPrecincts": (
                    summary_race["countedPrecincts"] if summary_race is not None else None
                ),
                "candidates": candidates,
                "precinctGroupCount": len(all_groups),
                "reportingGroupCount": sum(
                    1 for group in all_groups if group["reportingSplits"] > 0
                ),
                "precincts": precincts,
                "otherReportingGroups": other_groups,
            }
        )

    races.sort(key=race_sort_key)
    if strict_current_report and candidate_count != EXPECTED_CANDIDATES:
        raise ResultsError(
            f"Current report has {candidate_count} candidates; expected "
            f"{EXPECTED_CANDIDATES}"
        )
    registered_voters = sum(row["registeredVoters"] for row in turnout_precincts)
    ballots = sum(row["ballots"] for row in turnout_precincts)
    report_date = parse_iso_timestamp(report_timestamp).astimezone(HAWAII_TIME).date()
    report_label = (
        "Final precinct results"
        if report_status == "final"
        else "Preliminary precinct results"
    )
    publication = {
        "schemaVersion": SCHEMA_VERSION,
        "meta": {
            "election": election_title,
            "report": report_label,
            "status": report_status,
            "final": report_status == "final",
            "reportTimestamp": report_timestamp,
            "updatedAt": report_timestamp,
            "reportDate": report_date.isoformat(),
            "source": (
                "Hawaiʻi Office of Elections statewide summary, precinct detail text, and final precinct PDF"
                if summary is not None and pdf_source_url is not None
                else "Hawaiʻi Office of Elections statewide summary and precinct detail text files"
                if summary is not None
                else "Hawaiʻi Office of Elections precinct detail text file"
            ),
            "officialResultsPage": RESULTS_PAGE,
            "sourceUrl": source_url,
            "sourceSha256": source_sha256,
            "summarySourceUrl": summary_source_url,
            "summarySourceSha256": summary_source_sha256,
            "pdfUrl": pdf_source_url,
            "pdfSourceSha256": pdf_source_sha256,
            "rowCount": parsed.rows,
            "summaryRowCount": summary.rows if summary is not None else 0,
            "raceCount": len(races),
            "candidateCount": candidate_count,
            "mappedPrecinctCount": len(mapped_groups),
            "reportingGroupCount": len(parsed.group_splits),
            "splitCount": len(parsed.splits),
            "unmappedReportingGroups": unmapped_groups,
            "turnout": {
                "registeredVoters": registered_voters,
                "ballots": ballots,
                "rate": round((ballots / registered_voters) * 100, 2)
                if registered_voters
                else 0.0,
            },
        },
        "turnout": {
            "precincts": turnout_precincts,
            "otherReportingGroups": turnout_unmapped,
        },
        "levels": list(LEVELS),
        "races": races,
    }
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "election": election_title,
        "reportTimestamp": report_timestamp,
        "sourceUrl": source_url,
        "sha256": source_sha256,
        "bytes": source_bytes,
        "summarySourceUrl": summary_source_url,
        "summarySha256": summary_source_sha256,
        "summaryBytes": summary_source_bytes,
        "pdfUrl": pdf_source_url,
        "pdfSha256": pdf_source_sha256,
        "pdfBytes": pdf_source_bytes,
        "reportStatus": report_status,
        "rowCount": parsed.rows,
        "summaryRowCount": summary.rows if summary is not None else 0,
        "raceCount": len(races),
        "candidateCount": candidate_count,
        "mappedPrecinctCount": len(mapped_groups),
        "reportingGroupCount": len(parsed.group_splits),
        "splitCount": len(parsed.splits),
    }
    return publication, manifest


def fetch_bytes(url: str, *, accept: str, attempts: int = 3) -> tuple[bytes, dict[str, str]]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "HawaiiPrecinctResultsUpdater/1.0 (+https://github.com/davinaoyagi-arch/ballots)",
                "Accept": accept,
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status != 200:
                    raise ResultsError(f"{url}: HTTP {response.status}")
                return response.read(), {key.lower(): value for key, value in response.headers.items()}
        except (OSError, urllib.error.URLError, ResultsError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 2)
    raise ResultsError(f"{url}: download failed after {attempts} attempts: {last_error}")


def discover_source_urls(page_html: str, election_label: str) -> tuple[str, str]:
    parser = ResultsPageParser()
    parser.feed(page_html)
    matches: dict[str, list[tuple[str, str]]] = {"media": [], "summary": []}
    for href in parser.links:
        url = urllib.parse.urljoin(RESULTS_PAGE, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() != "https" or parsed.hostname != RESULTS_HOST:
            continue
        media_match = MEDIA_PATH_PATTERN.fullmatch(parsed.path)
        summary_match = SUMMARY_PATH_PATTERN.fullmatch(parsed.path)
        match = media_match or summary_match
        if match is None:
            continue
        label = urllib.parse.unquote(match.group(1))
        encoded_path = urllib.parse.quote(parsed.path, safe="/%")
        normalized_url = urllib.parse.urlunparse(parsed._replace(path=encoded_path))
        matches["media" if media_match is not None else "summary"].append(
            (label, normalized_url)
        )

    selected_urls: dict[str, str] = {}
    for kind in ("media", "summary"):
        selected = [
            url
            for label, url in matches[kind]
            if label.casefold() == election_label.casefold()
        ]
        if len(selected) != 1:
            found = ", ".join(sorted(label for label, _ in matches[kind])) or "none"
            raise ResultsError(
                f"Expected one {election_label!r} {kind}.txt link; found "
                f"{len(selected)}. Available labels: {found}"
            )
        selected_urls[kind] = selected[0]
    return selected_urls["media"], selected_urls["summary"]


def discover_source_url(page_html: str, election_label: str) -> str:
    return discover_source_urls(page_html, election_label)[0]


def parse_iso_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResultsError(f"Invalid report timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HAWAII_TIME)
    return parsed


def source_timestamp(headers: dict[str, str]) -> datetime:
    last_modified = headers.get("last-modified")
    if last_modified:
        try:
            parsed = parsedate_to_datetime(last_modified)
        except (TypeError, ValueError) as exc:
            raise ResultsError(f"Invalid Last-Modified header {last_modified!r}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return datetime.now(timezone.utc).replace(microsecond=0)


def reset_diagnostics_directory(path: Path) -> None:
    resolved = path.resolve()
    workspace = Path.cwd().resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ResultsError("Diagnostics directory must be inside the working directory") from exc
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def validate_official_source_url(url: str, label: str) -> None:
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.scheme.lower() != "https" or parsed_url.hostname != RESULTS_HOST:
        raise ResultsError(
            f"{label} source URL must use HTTPS on elections.hawaii.gov"
        )


def load_sources(args: argparse.Namespace) -> LoadedSources:
    if args.local_file:
        if not args.local_file.is_file():
            raise ResultsError(f"Local source does not exist: {args.local_file}")
        precinct = args.local_file.read_bytes()
        precinct_timestamp = (
            parse_iso_timestamp(args.report_timestamp)
            if args.report_timestamp
            else datetime.fromtimestamp(args.local_file.stat().st_mtime, HAWAII_TIME)
        )
        summary: bytes | None = None
        summary_url: str | None = None
        summary_timestamp: datetime | None = None
        if args.local_summary_file:
            if not args.local_summary_file.is_file():
                raise ResultsError(
                    f"Local summary source does not exist: {args.local_summary_file}"
                )
            summary = args.local_summary_file.read_bytes()
            summary_url = (
                args.summary_source_url or args.local_summary_file.resolve().as_uri()
            )
            summary_timestamp = (
                parse_iso_timestamp(args.report_timestamp)
                if args.report_timestamp
                else datetime.fromtimestamp(
                    args.local_summary_file.stat().st_mtime, HAWAII_TIME
                )
            )
        return LoadedSources(
            precinct=precinct,
            precinct_url=args.source_url or args.local_file.resolve().as_uri(),
            precinct_timestamp=precinct_timestamp,
            summary=summary,
            summary_url=summary_url,
            summary_timestamp=summary_timestamp,
            mode="local",
        )
    if args.local_summary_file:
        raise ResultsError("--local-summary-file requires --local-file")

    page_bytes, _ = fetch_bytes(
        f"{RESULTS_PAGE}?refresh={int(time.time())}", accept="text/html,*/*;q=0.8"
    )
    page_html = page_bytes.decode("utf-8", errors="replace")
    (args.diagnostics_dir / "source-page.html").write_text(page_html, encoding="utf-8")
    discovered_precinct_url, discovered_summary_url = discover_source_urls(
        page_html, args.election_label
    )
    precinct_url = args.source_url or discovered_precinct_url
    summary_url = args.summary_source_url or discovered_summary_url
    validate_official_source_url(precinct_url, "Precinct")
    validate_official_source_url(summary_url, "Summary")

    def refreshed(url: str) -> str:
        parsed_url = urllib.parse.urlparse(url)
        separator = "&" if parsed_url.query else "?"
        return f"{url}{separator}refresh={int(time.time())}"

    precinct, precinct_headers = fetch_bytes(
        refreshed(precinct_url), accept="text/plain,*/*;q=0.8"
    )
    summary, summary_headers = fetch_bytes(
        refreshed(summary_url), accept="text/plain,*/*;q=0.8"
    )
    (args.diagnostics_dir / "source-media.txt").write_bytes(precinct)
    (args.diagnostics_dir / "source-summary.txt").write_bytes(summary)
    return LoadedSources(
        precinct=precinct,
        precinct_url=precinct_url,
        precinct_timestamp=source_timestamp(precinct_headers),
        summary=summary,
        summary_url=summary_url,
        summary_timestamp=source_timestamp(summary_headers),
        mode="network",
    )


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ResultsError(f"Could not read existing {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ResultsError(f"Existing {path} is not a JSON object")
    return value


def publication_keys(publication: dict[str, Any]) -> tuple[set[str], set[str]]:
    races = publication.get("races")
    if not isinstance(races, list):
        raise ResultsError("Existing precinct publication has no races array")
    contest_ids: set[str] = set()
    choice_keys: set[str] = set()
    for race in races:
        if not isinstance(race, dict) or not isinstance(race.get("candidates"), list):
            raise ResultsError("Existing precinct publication has an invalid race")
        contest_id = str(race.get("contestId"))
        contest_ids.add(contest_id)
        for candidate in race["candidates"]:
            if not isinstance(candidate, dict):
                raise ResultsError("Existing precinct publication has an invalid candidate")
            choice_keys.add(f"{contest_id}|{candidate.get('choiceId')}")
    return contest_ids, choice_keys


def validate_previous_publication(
    previous: dict[str, Any] | None, publication: dict[str, Any]
) -> None:
    if not previous:
        return
    previous_meta = previous.get("meta")
    current_meta = publication.get("meta")
    if not isinstance(previous_meta, dict) or not isinstance(current_meta, dict):
        raise ResultsError("Existing precinct publication has invalid metadata")
    previous_timestamp = parse_iso_timestamp(str(previous_meta.get("reportTimestamp")))
    current_timestamp = parse_iso_timestamp(str(current_meta.get("reportTimestamp")))
    if current_timestamp < previous_timestamp:
        raise ResultsError(
            f"Refusing to replace newer precinct results {previous_timestamp.isoformat()} "
            f"with {current_timestamp.isoformat()}"
        )
    old_contests, old_choices = publication_keys(previous)
    new_contests, new_choices = publication_keys(publication)
    if old_contests != new_contests or old_choices != new_choices:
        raise ResultsError(
            "Contest or choice keyset changed. Refusing a potentially incomplete export."
        )


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
    sources = load_sources(args)
    precinct_registry = load_precinct_registry(args.precinct_registry)
    parsed = parse_precinct_export(sources.precinct)
    summary = parse_summary_export(sources.summary) if sources.summary is not None else None
    source_hash = hashlib.sha256(sources.precinct).hexdigest()
    summary_hash = (
        hashlib.sha256(sources.summary).hexdigest()
        if sources.summary is not None
        else None
    )
    previous_publication = load_json(args.output)
    timestamps = [sources.precinct_timestamp]
    if sources.summary_timestamp is not None:
        timestamps.append(sources.summary_timestamp)
    timestamps.append(parse_iso_timestamp(FINAL_REPORT_TIMESTAMP))
    report_timestamp = max(timestamps).isoformat(timespec="seconds")
    if previous_publication:
        previous_meta = previous_publication.get("meta")
        if (
            isinstance(previous_meta, dict)
            and previous_meta.get("sourceSha256") == source_hash
            and previous_meta.get("summarySourceSha256") == summary_hash
            and previous_meta.get("pdfSourceSha256") == FINAL_PRECINCT_PDF_SHA256
            and isinstance(previous_meta.get("reportTimestamp"), str)
        ):
            report_timestamp = previous_meta["reportTimestamp"]
    publication, manifest = build_publication(
        parsed,
        summary=summary,
        election_title=args.election_title,
        report_timestamp=report_timestamp,
        source_url=sources.precinct_url,
        source_sha256=source_hash,
        source_bytes=len(sources.precinct),
        summary_source_url=sources.summary_url,
        summary_source_sha256=summary_hash,
        summary_source_bytes=(
            len(sources.summary) if sources.summary is not None else None
        ),
        pdf_source_url=FINAL_PRECINCT_PDF_URL,
        pdf_source_sha256=FINAL_PRECINCT_PDF_SHA256,
        pdf_source_bytes=FINAL_PRECINCT_PDF_BYTES,
        report_status="final",
        precinct_registry=precinct_registry,
        strict_current_report=True,
    )
    validate_previous_publication(previous_publication, publication)

    publication_text = stable_json(publication)
    manifest_text = stable_json(manifest)
    existing_publication_text = (
        args.output.read_text(encoding="utf-8") if args.output.is_file() else None
    )
    existing_manifest_text = (
        args.manifest.read_text(encoding="utf-8") if args.manifest.is_file() else None
    )
    existing_public_text = (
        args.public_output.read_text(encoding="utf-8")
        if args.public_output.is_file()
        else None
    )
    data_changed = (
        publication_text != existing_publication_text
        or manifest_text != existing_manifest_text
        or publication_text != existing_public_text
    )

    previous_status = load_json(args.status)
    today_hst = datetime.now(HAWAII_TIME).date()
    heartbeat_due = should_refresh_heartbeat(previous_status, today_hst)
    status_changed = data_changed or heartbeat_due
    if status_changed:
        status = {
            "schemaVersion": SCHEMA_VERSION,
            "lastSuccessfulCheckDateHst": today_hst.isoformat(),
            "latestReportTimestamp": publication["meta"]["reportTimestamp"],
            "sourceSha256": source_hash,
            "summarySourceSha256": summary_hash,
            "pdfSourceSha256": FINAL_PRECINCT_PDF_SHA256,
            "reportStatus": publication["meta"]["status"],
            "raceCount": publication["meta"]["raceCount"],
            "candidateCount": publication["meta"]["candidateCount"],
            "mode": sources.mode,
        }
        status_text = stable_json(status)
    else:
        status_text = args.status.read_text(encoding="utf-8")

    changed_paths: list[str] = []
    if atomic_write_if_changed(args.output, publication_text):
        changed_paths.append(str(args.output))
    if atomic_write_if_changed(args.public_output, publication_text):
        changed_paths.append(str(args.public_output))
    if atomic_write_if_changed(args.manifest, manifest_text):
        changed_paths.append(str(args.manifest))
    if atomic_write_if_changed(args.status, status_text):
        changed_paths.append(str(args.status))

    print(
        f"Validated {publication['meta']['raceCount']} races, "
        f"{publication['meta']['candidateCount']} choices, and "
        f"{publication['meta']['mappedPrecinctCount']} mapped precincts from "
        f"{parsed.rows:,} rows."
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
