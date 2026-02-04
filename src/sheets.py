from __future__ import annotations

import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd


class SheetsWriter:
    """
    Important: Google Sheets has a 10,000,000 cell limit per spreadsheet.
    We create new worksheets SMALL to avoid hitting the limit.
    """

    def __init__(self, sheet_name: str, credentials_path: str) -> None:
        scope = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = ServiceAccountCredentials.from_json_keyfile_name(credentials_path, scope)
        self.client = gspread.authorize(creds)
        self.book = self.client.open(sheet_name)

    def _upsert_worksheet(self, title: str, rows: int = 2000, cols: int = 26):
        """
        Create/find a worksheet. Keep it small by default.
        """
        try:
            ws = self.book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.book.add_worksheet(title=title, rows=rows, cols=cols)
        return ws

    def _ensure_size(self, ws, rows_needed: int, cols_needed: int):
        """
        Resize worksheet only if needed, but do it cautiously.
        """
        current_rows = ws.row_count
        current_cols = ws.col_count

        new_rows = current_rows
        new_cols = current_cols

        if rows_needed > current_rows:
            new_rows = min(max(rows_needed + 50, current_rows), 50000)  # hard cap
        if cols_needed > current_cols:
            new_cols = min(max(cols_needed + 5, current_cols), 50)      # hard cap

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
        rows_needed = len(out) + 1
        cols_needed = len(out.columns)

        self._ensure_size(ws, rows_needed, cols_needed)
        ws.update([out.columns.tolist()] + out.astype(str).values.tolist())

    def append_df(self, title: str, df: pd.DataFrame) -> None:
        ws = self._upsert_worksheet(title)

        if df is None or df.empty:
            return

        out = df.copy().fillna("")
        cols_needed = len(out.columns)

        existing = ws.get_all_values()
        if not existing:
            # create header + rows
            self._ensure_size(ws, len(out) + 1, cols_needed)
            ws.update([out.columns.tolist()] + out.astype(str).values.tolist())
        else:
            # append rows only
            # ensure columns fit
            self._ensure_size(ws, len(existing) + len(out) + 1, cols_needed)
            ws.append_rows(out.astype(str).values.tolist(), value_input_option="USER_ENTERED")

    def read_df(self, title: str) -> pd.DataFrame:
        ws = self._upsert_worksheet(title)
        values = ws.get_all_values()
        if not values or len(values) < 2:
            return pd.DataFrame()
        header = values[0]
        rows = values[1:]
        return pd.DataFrame(rows, columns=header)
