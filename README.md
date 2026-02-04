# Horse Racing Google Sheet (Automated via GitHub Actions)

This repo runs a Python job on a schedule (GitHub Actions) and writes UK/IRE racecards + odds + model scores into Google Sheets.

## What it does
- Pulls today's race list + race detail from irishracing.com racecards
- Pulls best-available price per runner from irishracing.com odds-comparison pages
- Calculates simple model scores (form/recency/course-distance/market)
- Publishes to Google Sheets tabs:
  - TODAYS_RACES
  - RUNNERS
  - VALUE_BETS
  - RUN_LOG

## Required GitHub Secrets
Create these in: Repo → Settings → Secrets and variables → Actions

- `GOOGLE_CREDS`  (paste the entire service-account JSON)
- `SHEET_NAME`    (e.g. `Horse Racing Model`)

Optional:
- `REGION`        (`gb`, `ire`, or `all`) default `all`
- `MIN_VALUE_EDGE` (e.g. `0.03` meaning model prob - market prob >= 3%) default `0.02`

## Local test (optional)
1) Put your service-account json as `credentials.json` in repo root  
2) `pip install -r requirements.txt`  
3) `python horse_model.py`

## Notes / reliability
This uses public web pages and may break if the site markup changes. If you want a **commercial-grade** setup, use a paid data feed (e.g. The Racing API). Their docs are here: https://api.theracingapi.com/documentation
