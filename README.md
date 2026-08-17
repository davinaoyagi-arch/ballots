# Hawaiʻi Ballot Return Data

This repository is the always-on data source for the public Hawaiʻi precinct
map. GitHub Actions checks the Hawaiʻi Office of Elections report page four
times a day, downloads the newest complete five-PDF set, validates every
precinct and county total, and publishes JSON only when the source changes.

Public data endpoint:

`https://raw.githubusercontent.com/davinaoyagi-arch/ballots/main/data/ballot-returns.json`

Precinct election-results endpoint:

`https://raw.githubusercontent.com/davinaoyagi-arch/ballots/main/data/precinct-results.json`

## Safety checks

The updater will keep the last known-good data if any check fails. It requires:

- one complete statewide plus four-county report set from a single date;
- exactly 247 county/precinct rows with the established keyset;
- nonnegative integer values and no duplicate precincts;
- each row's reported `VOTED` and `TOTAL` formulas to reconcile;
- every county summary to match its precinct rows; and
- county PDF totals to match the statewide PDF.

Corrections published under the same report date are accepted when the PDF
hashes change and all validations still pass. Older reports never replace newer
ones.

## Run it manually

Open **Actions → Refresh Hawaii ballot reports → Run workflow**. Scheduled runs
use GitHub's temporary `GITHUB_TOKEN`; no personal token or always-on computer
is required.

## Test with local PDFs

```powershell
python automation/update_reports.py `
  --local-pdf "HAWAII=C:\path\Hawaii Island.pdf" `
  --local-pdf "MAUI=C:\path\Maui County.pdf" `
  --local-pdf "KAUAI=C:\path\Kauai.pdf" `
  --local-pdf "OAHU=C:\path\Honolulu.pdf"
```

## Precinct election results

`automation/update_precinct_results.py` discovers the current statewide summary
and precinct-detail text files on the Office of Elections results page. It
parses both tab-delimited exports with CSV quoting support, discovers races and
choices by contest and choice ID, and combines mail and in-person votes. Overall
race totals come from the statewide summary as soon as they are published;
precinct maps use the geographic detail file and aggregate MAIL and VSC split
records without repeating turnout counts. The publication is reconciled to the
Office of Elections final precinct PDF printed August 14, 2026 and links that
report as its official human-readable source.

Test a downloaded export locally:

```powershell
python automation/update_precinct_results.py `
  --local-file "C:\path\Precinct.txt" `
  --local-summary-file "C:\path\summary.txt"
```
