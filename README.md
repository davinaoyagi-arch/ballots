# Hawaiʻi Ballot Return Data

This repository is the always-on data source for the public Hawaiʻi precinct
map. GitHub Actions checks the Hawaiʻi Office of Elections report page four
times a day, downloads the newest complete five-PDF set, validates every
precinct and county total, and publishes JSON only when the source changes.

Public data endpoint:

`https://raw.githubusercontent.com/davinaoyagi-arch/ballots/main/data/ballot-returns.json`

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
