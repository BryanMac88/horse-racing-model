from __future__ import annotations

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd


class SheetsWriter:
    def __init__(self, sheet_name: str, credentials_path: str) -> None:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
        self.client = gspread.authorize(creds)
        self.book = self.client.open(sheet_name)

    def _upsert_worksheet(self, title: str, rows: int = 2000, cols: int = 30):
        try:
            ws = self.book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.book.add_worksheet(title=title, rows=rows, cols=cols)
        return ws

    def _write_df(self, title: str, df: pd.DataFrame) -> None:
        ws = self._upsert_worksheet(title)
        ws.clear()
        if df is None or df.empty:
            ws.update([["(no data)"]])
            return
        df = df.copy()
        # keep it Sheets-friendly
        df = df.fillna("")
        ws.update([df.columns.tolist()] + df.astype(str).values.tolist())

    def write_all(self, races: pd.DataFrame, runners: pd.DataFrame, value_bets: pd.DataFrame, run_log: pd.DataFrame) -> None:
        self._write_df("TODAYS_RACES", races)
        self._write_df("RUNNERS", runners)
        self._write_df("VALUE_BETS", value_bets)
        self._write_df("RUN_LOG", run_log)
