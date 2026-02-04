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

    def _upsert_worksheet(self, title: str, rows: int = 50000, cols: int = 50):
        try:
            ws = self.book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.book.add_worksheet(title=title, rows=rows, cols=cols)
        return ws

    def write_df(self, title: str, df: pd.DataFrame) -> None:
        ws = self._upsert_worksheet(title)
        ws.clear()
        if df is None or df.empty:
            ws.update([["(no data)"]])
            return
        out = df.copy().fillna("")
        ws.update([out.columns.tolist()] + out.astype(str).values.tolist())

    def append_df(self, title: str, df: pd.DataFrame) -> None:
        ws = self._upsert_worksheet(title)
        if df is None or df.empty:
            return
        out = df.copy().fillna("")
        existing = ws.get_all_values()
        if not existing:
            ws.update([out.columns.tolist()] + out.astype(str).values.tolist())
        else:
            ws.append_rows(out.astype(str).values.tolist(), value_input_option="USER_ENTERED")

    def read_df(self, title: str) -> pd.DataFrame:
        """Read a worksheet into a DataFrame (best-effort)."""
        ws = self._upsert_worksheet(title)
        values = ws.get_all_values()
        if not values or len(values) < 2:
            return pd.DataFrame()
        header = values[0]
        rows = values[1:]
        return pd.DataFrame(rows, columns=header)
