from __future__ import annotations

import json
import os

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


class SheetsWriter:
    def __init__(self, sheet_name: str, credentials_path: str = "credentials.json") -> None:
        creds = None
        if credentials_path and os.path.exists(credentials_path):
            creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        else:
            raw = os.getenv("GOOGLE_CREDS", "")
            if raw.strip():
                info = json.loads(raw)
                creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        if creds is None:
            raise RuntimeError(
                "No Google credentials found. Provide credentials.json or set GOOGLE_CREDS."
            )
        self.client = gspread.authorize(creds)
        self.book = self.client.open(sheet_name)

    def _upsert_worksheet(self, title: str, rows: int = 2000, cols: int = 30):
        try:
            ws = self.book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.book.add_worksheet(title=title, rows=rows, cols=cols)
        return ws

    def _ensure_size(self, ws, rows_needed: int, cols_needed: int):
        current_rows = ws.row_count
        current_cols = ws.col_count
        new_rows = current_rows
        new_cols = current_cols
        if rows_needed > current_rows:
            new_rows = min(max(rows_needed + 100, current_rows), 50000)
        if cols_needed > current_cols:
            new_cols = min(max(cols_needed + 5, current_cols), 60)
        if new_rows != current_rows or new_cols != current_cols:
            ws.resize(rows=new_rows, cols=new_cols)

    def write_df(self, title: str, df: pd.DataFrame) -> None:
        ws = self._upsert_worksheet(title)
        ws.clear()
        if df is None or df.empty:
            self._ensure_size(ws, 2, 2)
            ws.update([["(no data)"]])
            return
        out = df.copy().fillna("")
        self._ensure_size(ws, len(out) + 1, len(out.columns))
        ws.update([out.columns.tolist()] + out.astype(str).values.tolist())

    def append_df(self, title: str, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        ws = self._upsert_worksheet(title)
        out = df.copy().fillna("")
        existing = ws.get_all_values()
        if not existing:
            self._ensure_size(ws, len(out) + 1, len(out.columns))
            ws.update([out.columns.tolist()] + out.astype(str).values.tolist())
        else:
            self._ensure_size(
                ws,
                len(existing) + len(out) + 1,
                max(len(out.columns), len(existing[0]) if existing else 1),
            )
            ws.append_rows(out.astype(str).values.tolist(), value_input_option="USER_ENTERED")

    def read_df(self, title: str) -> pd.DataFrame:
        ws = self._upsert_worksheet(title)
        values = ws.get_all_values()
        if not values or len(values) < 2:
            return pd.DataFrame()
        return pd.DataFrame(values[1:], columns=values[0])
