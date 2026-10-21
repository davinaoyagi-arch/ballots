import unittest

from automation.update_reports import (
    ReportError,
    calculate_rate,
    reconcile_statewide_county_summary,
)


class StatewideReconciliationTests(unittest.TestCase):
    def test_rate_rounds_half_up_like_the_browser(self) -> None:
        self.assertEqual(calculate_rate(33, 800), 4.13)

    def test_exact_county_summary_needs_no_warning(self) -> None:
        summary = [466, 127, 0, 0, 111182, 6890, 51, 7017, 111648]
        self.assertIsNone(
            reconcile_statewide_county_summary("HAWAII", summary, summary.copy())
        )

    def test_one_ballot_mail_sent_difference_is_audited(self) -> None:
        detail = [466, 127, 0, 0, 111182, 6890, 51, 7017, 111648]
        statewide = [466, 127, 0, 0, 111183, 6890, 51, 7017, 111649]
        warning = reconcile_statewide_county_summary("HAWAII", detail, statewide)
        self.assertEqual(warning["difference"], 1)
        self.assertEqual(warning["publishedUsing"], "county D/P detail")

    def test_returned_ballot_difference_still_fails_closed(self) -> None:
        detail = [466, 127, 0, 0, 111182, 6890, 51, 7017, 111648]
        statewide = [466, 127, 0, 0, 111182, 6891, 51, 7018, 111648]
        with self.assertRaises(ReportError):
            reconcile_statewide_county_summary("HAWAII", detail, statewide)

    def test_more_than_one_sent_ballot_difference_fails_closed(self) -> None:
        detail = [466, 127, 0, 0, 111182, 6890, 51, 7017, 111648]
        statewide = [466, 127, 0, 0, 111184, 6890, 51, 7017, 111650]
        with self.assertRaises(ReportError):
            reconcile_statewide_county_summary("HAWAII", detail, statewide)


if __name__ == "__main__":
    unittest.main()
