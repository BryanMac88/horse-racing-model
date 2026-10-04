# Horse Racing Google Sheet (Automated via GitHub Actions)

This repo runs a Python job on a schedule and writes UK/IRE racecards + odds + model scores + market movers into Google Sheets.

## What it does

- Pulls today’s (or tomorrow’s after 22:00 Dublin) races from irishracing.com
- Uses Probable SP as the price
- Calculates model probability and value edge
- Tracks market movers (shortening / drifting) from historical snapshots
- Produces a shortlist of bets

### Main tabs in the Google Sheet

| Tab | Purpose |
|-----|---------|
| `BETS_TO_PLACE` | **Your daily shortlist** – start here |
| `SIGNALS` | Full ranking with value edge + shortening scores |
| `MARKET_MOVERS_2H` | Recent price movers |
| `MARKET_MOVERS` | Night-before to 30 min before movers |
| `VALUE_BETS_TARGET` | Pure value edge list |
| `RUNNERS_TARGET` | All runners with scores |
| `RACES_TARGET` | Race list for the target day |
| `DASHBOARD` | Quick status of the last run |
| `RUN_LOG` | History of runs |
| `BET_RECS_LOG` | Log of suggested bets (fill result/pnl later) |
| `TARGET_DAY` | Which day the job is targeting |
| `MARKET_SNAPSHOTS_LOG` | Historical price snapshots |

## Required GitHub Secrets

Repo → Settings → Secrets and variables → Actions

| Secret | Required | Recommended value | Notes |
|--------|----------|-------------------|-------|
| `GOOGLE_CREDS` | Yes | Full service-account JSON | Paste entire JSON |
| `SHEET_NAME` | Yes | `Horse Racing Model` | Exact spreadsheet title |
| `REGION` | No | `ire` | `ire`, `gb`, or `all` |
| `MIN_VALUE_EDGE` | No | `0.02` | Minimum model edge (e.g. 0.02 = 2%) |

## Recommended daily process

1. Let the scheduled workflow run for a few hours so snapshots build up.
2. Open the Google Sheet 30–60 minutes before the races you care about.
3. Go to the **`BETS_TO_PLACE`** tab first.
4. Prefer horses that have:
   - Positive `value_edge`, **and**
   - Negative `mover_2h_pct` or `mover_night_pct` (shortening).
5. Always check the **current real odds** with a bookmaker / Betfair before staking.
6. After the race, fill `result` and `pnl_units` in the **`BET_RECS_LOG`** tab so you can measure performance.

## Local test

1. Put your service-account JSON as `credentials.json` in the repo root  
   (or set the `GOOGLE_CREDS` and `SHEET_NAME` environment variables)
2. `pip install -r requirements.txt`
3. `python horse_model.py`

## Notes

- This uses public web pages and may break if irishracing.com changes its layout.
- Probable SP is an estimate, not the best available bookmaker price.
- For commercial-grade data consider a paid feed such as [The Racing API](https://api.theracingapi.com/documentation).
