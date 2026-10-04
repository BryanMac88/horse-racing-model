# Horse Racing Google Sheet (Automated via GitHub Actions)

This repo runs a Python job on a schedule (GitHub Actions) and writes UK/IRE racecards + odds + model scores into Google Sheets.

## What it does

- Pulls today's (or tomorrow's after 22:00 Dublin) race list + runners from irishracing.com
- Uses Probable SP as best-available price
- Calculates model scores + value edge
- Tracks market movers (shortening / drifting) from historical snapshots
- Publishes to Google Sheets tabs:
  - `TARGET_DAY` – which day the job is targeting
  - `RACES_TARGET`
  - `RUNNERS_TARGET`
  - `VALUE_BETS_TARGET`
  - `MARKET_SNAPSHOTS_TARGET` / `MARKET_SNAPSHOTS_LOG`
  - `MARKET_MOVERS_2H` / `MARKET_MOVERS`
  - `SIGNALS`
  - `BETS_TO_PLACE`
  - `BET_RECS_LOG`
  - `DASHBOARD`
  - `RUN_LOG`

## Required GitHub Secrets

Repo → Settings → Secrets and variables → Actions

| Secret            | Required | Example              | Notes                          |
|-------------------|----------|----------------------|--------------------------------|
| `GOOGLE_CREDS`    | Yes      | full service-account JSON | paste entire JSON             |
| `SHEET_NAME`      | Yes      | `Horse Racing Model` | exact name of the spreadsheet |
| `REGION`          | No       | `all` / `gb` / `ire` | default `all`                  |
| `MIN_VALUE_EDGE`  | No       | `0.02`               | default `0.00`                 |

## Local test

1. Put your service-account JSON as `credentials.json` in the repo root  
   (or export `GOOGLE_CREDS` + `SHEET_NAME`)
2. `pip install -r requirements.txt`
3. `python horse_model.py`

## Notes / reliability

This uses public web pages and may break if the site markup changes.  
For a commercial-grade feed consider [The Racing API](https://api.theracingapi.com/documentation).

After the first few runs the mover tabs will start filling as snapshots accumulate.
