from __future__ import annotations

import csv
import importlib.util
import io
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "update_precinct_results.py"
SPEC = importlib.util.spec_from_file_location("update_precinct_results", MODULE_PATH)
assert SPEC and SPEC.loader
results = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = results
SPEC.loader.exec_module(results)


HEADERS = [
    "#Precinct_Name",
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
]

SUMMARY_HEADERS = [
    "#Contest ID",
    "Contest Title",
    "Contest Seq Nbr",
    "Contest Type",
    "Contest Party",
    "Mail Blank Votes",
    "In-Person Blank Votes",
    "Mail Over Votes",
    "In-Person Over Votes",
    "Mail Invalid Votes",
    "In-Person Invalid Votes",
    "Registered Voters",
    "Total Precincts",
    "Counted Precincts",
    "Candidate ID",
    "Candidate Name",
    "Candidate Seq Nbr",
    "Candidate Party",
    "Mail Votes",
    "In-Person Votes",
    "Total Votes",
]


def make_export(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\r\n")
    writer.writerow(["#FormatVersion 1", *("" for _ in range(14))])
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def make_summary_export(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\r\n")
    writer.writerow(["#FormatVersion 1", *("" for _ in range(20))])
    writer.writerow(SUMMARY_HEADERS)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


class PrecinctResultsParserTests(unittest.TestCase):
    def fixture(self) -> bytes:
        return make_export(
            [
                [
                    "1-Jan",
                    "",
                    "1",
                    "100",
                    "40",
                    "1",
                    "10",
                    "Mayor",
                    "NON",
                    "1",
                    "DOE, Jane\n(JJ)",
                    "",
                    "C",
                    "20",
                    "0",
                ],
                [
                    "1-Jan",
                    "",
                    "1",
                    "100",
                    "40",
                    "1",
                    "10",
                    "Mayor",
                    "NON",
                    "2",
                    "SMITH, Alex",
                    "",
                    "C",
                    "10",
                    "0",
                ],
                [
                    "1-Jan",
                    "VSC",
                    "2",
                    "0",
                    "5",
                    "1",
                    "10",
                    "Mayor",
                    "NON",
                    "1",
                    "DOE, Jane\n(JJ)",
                    "",
                    "C",
                    "0",
                    "3",
                ],
                [
                    "1-Jan",
                    "VSC",
                    "2",
                    "0",
                    "5",
                    "1",
                    "10",
                    "Mayor",
                    "NON",
                    "2",
                    "SMITH, Alex",
                    "",
                    "C",
                    "0",
                    "2",
                ],
                [
                    "1-Jan",
                    "",
                    "1",
                    "100",
                    "40",
                    "1",
                    "11",
                    "Councilmember",
                    "NON",
                    "1",
                    "LEE, Taylor",
                    "",
                    "C",
                    "4",
                    "0",
                ],
            ]
        )

    def summary_fixture(self) -> bytes:
        return make_summary_export(
            [
                [
                    "10",
                    "Mayor",
                    "1",
                    "OF",
                    "NON",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "100",
                    "2",
                    "1",
                    "1",
                    "DOE, Jane\n(JJ)",
                    "1",
                    "",
                    "70",
                    "10",
                    "80",
                ],
                [
                    "10",
                    "Mayor",
                    "1",
                    "OF",
                    "NON",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "100",
                    "2",
                    "1",
                    "2",
                    "SMITH, Alex",
                    "2",
                    "",
                    "18",
                    "2",
                    "20",
                ],
                [
                    "11",
                    "Councilmember",
                    "2",
                    "OF",
                    "NON",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "0",
                    "100",
                    "1",
                    "1",
                    "1",
                    "LEE, Taylor",
                    "1",
                    "",
                    "9",
                    "1",
                    "10",
                ],
            ]
        )

    def test_multiline_fields_and_races_are_discovered_dynamically(self) -> None:
        parsed = results.parse_precinct_export(self.fixture())

        self.assertEqual(parsed.rows, 5)
        self.assertEqual(set(parsed.races), {"10", "11"})
        self.assertEqual(
            parsed.races["10"]["choices"]["1"]["name"], "DOE, Jane (JJ)"
        )

    def test_mail_and_vsc_splits_are_aggregated_once(self) -> None:
        parsed = results.parse_precinct_export(self.fixture())
        publication, _ = results.build_publication(
            parsed,
            election_title="Fixture Election",
            report_timestamp="2026-08-08T19:30:00-10:00",
            source_url="file:///fixture/Precinct.txt",
            source_sha256="fixture",
            source_bytes=len(self.fixture()),
            expected_mapped_precincts=None,
        )

        mayor = next(race for race in publication["races"] if race["contestId"] == "10")
        self.assertEqual(mayor["totalVotes"], 35)
        self.assertEqual([candidate["votes"] for candidate in mayor["candidates"]], [23, 12])
        self.assertEqual(mayor["precincts"][0]["votes"], [23, 12])
        self.assertEqual(mayor["precincts"][0]["leaderChoiceIds"], ["1"])

        turnout = publication["turnout"]["precincts"][0]
        self.assertEqual(turnout["dp"], "01-01")
        self.assertEqual(turnout["registeredVoters"], 100)
        self.assertEqual(turnout["ballots"], 45)
        self.assertEqual(turnout["turnoutRate"], 45.0)

    def test_statewide_summary_populates_overall_results_only(self) -> None:
        parsed = results.parse_precinct_export(self.fixture())
        summary = results.parse_summary_export(self.summary_fixture())
        publication, _ = results.build_publication(
            parsed,
            summary=summary,
            election_title="Fixture Election",
            report_timestamp="2026-08-08T20:00:00-10:00",
            source_url="file:///fixture/Precinct.txt",
            source_sha256="precinct-fixture",
            source_bytes=len(self.fixture()),
            summary_source_url="file:///fixture/summary.txt",
            summary_source_sha256="summary-fixture",
            summary_source_bytes=len(self.summary_fixture()),
            expected_mapped_precincts=None,
        )

        mayor = next(race for race in publication["races"] if race["contestId"] == "10")
        self.assertEqual(mayor["totalVotes"], 100)
        self.assertEqual([candidate["votes"] for candidate in mayor["candidates"]], [80, 20])
        self.assertEqual([candidate["percentage"] for candidate in mayor["candidates"]], [80.0, 20.0])
        self.assertEqual(mayor["overallTotalsSource"], "statewide-summary")
        self.assertEqual(mayor["officialCountedPrecincts"], 1)
        self.assertEqual(mayor["precincts"][0]["votes"], [23, 12])
        self.assertEqual(publication["meta"]["summaryRowCount"], 3)

    def test_candidate_colors_are_stable_and_unique(self) -> None:
        colors = [
            results.deterministic_candidate_color("136", str(choice_id))
            for choice_id in range(1, 27)
        ]

        self.assertEqual(len(colors), len(set(colors)))
        self.assertEqual(
            colors[0], results.deterministic_candidate_color("136", "1")
        )

    def test_precinct_name_normalization_matches_boundary_keys(self) -> None:
        self.assertEqual(results.normalize_precinct_name("1-Jan"), "01-01")
        self.assertEqual(results.normalize_precinct_name("10-May"), "05-10")
        self.assertEqual(results.normalize_precinct_name("41-04"), "41-04")
        self.assertEqual(results.normalize_precinct_name("OS II"), "OS II")


if __name__ == "__main__":
    unittest.main()
