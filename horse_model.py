from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
import pandas as pd

from src.sources.irishracing import IrishRacingClient
from src.scoring import build_runner_scores, build_value_bets
from src.sheets import SheetsWriter


def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name)
    if v is None or v == "":
        if default is None:
            raise RuntimeError(f"Missing required env var: {name}")
        return default
    return v


def main() -> int:
    sheet_name = env("SHEET_NAME")
    region = env("REGION", "all").lower()  # gb / ire / all
    min_edge = float(env("MIN_VALUE_EDGE", "0.02"))

    client = IrishRacingClient(region=region)

    # 1) Pull today's races & runners
    races_df, runners_df = client.fetch_today()

    if races_df.empty or runners_df.empty:
        print("No races/runners found. Exiting.")
        return 0

    # 2) Enrich with best price
    runners_df = client.enrich_with_best_prices(runners_df)

    # 3) Score
    scored = build_runner_scores(runners_df)
    value_bets = build_value_bets(scored, min_edge=min_edge)

    # 4) Write to Google Sheets
    writer = SheetsWriter(sheet_name=sheet_name, credentials_path="credentials.json")
    writer.write_all(
        races=races_df,
        runners=scored,
        value_bets=value_bets,
        run_log=pd.DataFrame([{
            "run_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "region": region,
            "races": int(len(races_df)),
            "runners": int(len(scored)),
            "value_bets": int(len(value_bets)),
        }])
    )

    print("Update complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
