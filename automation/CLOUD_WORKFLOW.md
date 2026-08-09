# Hawaiʻi election data workflows

This project uses GitHub Actions as its always-on data source. No personal
computer needs to remain running.

## Ballot-return reports

`refresh-reports.yml` checks the Office of Elections absentee-reconciliation
reports four times a day and publishes:

`https://raw.githubusercontent.com/davinaoyagi-arch/ballots/main/data/ballot-returns.json`

The updater accepts a new four-county report set only after all 247 precincts,
county totals, statewide totals, and sent/returned formulas reconcile.

## Official precinct election results

`refresh-precinct-results.yml` checks the official 2026 Primary results every
15 minutes. It downloads the `media.txt` precinct-detail export and the
`summary.txt` statewide totals linked from the Office of Elections results
page, then publishes:

`https://raw.githubusercontent.com/davinaoyagi-arch/ballots/main/data/precinct-results.json`

The same validated document is written to
`public/data/precinct-results.json` as the map's embedded fallback.

The results updater fails closed unless the current report has:

- the exact established 247 mapped D/P keys and county assignments;
- 249 reporting groups, consisting of the map keys plus `OS I` and `OS II`;
- 496 Mail/VSC reporting splits;
- 136 contests and 293 candidates across 25,729 precinct-detail rows;
- candidate totals that exactly reconcile between `media.txt` and
  `summary.txt`; and
- stable, unique candidate colors within every contest.

Every contest is classified as U.S. House, statewide, State Senate, State
House, county, or Office of Hawaiian Affairs. County contests also carry their
applicable county, and all mapped result rows carry the county joined from the
validated ballot-return precinct registry. Older exports or changed
contest/candidate keysets never replace the last known-good feed.

## Manual runs

Open **Actions**, choose the relevant refresh workflow, and select **Run
workflow**. Scheduled runs use GitHub's temporary `GITHUB_TOKEN`; no personal
token is needed.

To rebuild the official results feed from downloaded exports:

```powershell
python automation/update_precinct_results.py `
  --local-file "C:\path\media.txt" `
  --local-summary-file "C:\path\summary.txt" `
  --source-url "https://elections.hawaii.gov/wp-content/results/2026%20Primary/media.txt" `
  --summary-source-url "https://elections.hawaii.gov/wp-content/results/2026%20Primary/summary.txt" `
  --report-timestamp "2026-08-09T07:17:10+00:00"
```

For ballot-return PDF testing:

```powershell
python automation/update_reports.py `
  --local-pdf "HAWAII=C:\path\Hawaii Island.pdf" `
  --local-pdf "MAUI=C:\path\Maui County.pdf" `
  --local-pdf "KAUAI=C:\path\Kauai.pdf" `
  --local-pdf "OAHU=C:\path\Honolulu.pdf"
```
