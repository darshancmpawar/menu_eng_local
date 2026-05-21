"""Excel-backed database — drop-in replacement for the Supabase client.

Implements the same chainable query-builder interface used throughout the
codebase (sb.table('x').select('a,b').eq('col', val).execute()) so no
caller needs to change. Each table lives in its own .xlsx file under
data/database/. Files are created automatically if they don't exist.

Supported operations: select, insert, update, delete, upsert
Supported filters:   eq, in_, gte
Supported modifiers: order, limit, maybe_single
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Table schemas
# ---------------------------------------------------------------------------

_TABLES: Dict[str, Dict] = {
    "menu_categories": {
        "columns": ["name", "slots"],
        "pk": "name",
        "json_columns": ["slots"],  # Python list stored as JSON string
        "int_columns": [],
        "defaults": {},
    },
    "clients": {
        "columns": ["name", "menu_category", "version", "created_at"],
        "pk": "name",
        "json_columns": [],
        "int_columns": ["version"],
        "defaults": {"version": 1},
    },
    "slot_count_overrides": {
        "columns": ["client_name", "slot", "count"],
        "pk": None,
        "json_columns": [],
        "int_columns": ["count"],
        "defaults": {},
    },
    "theme_overrides": {
        "columns": ["client_name", "day", "theme"],
        "pk": None,
        "json_columns": [],
        "int_columns": [],
        "defaults": {},
    },
    "app_settings": {
        "columns": ["key", "value"],
        "pk": "key",
        "json_columns": [],  # value stays as string; caller parses it
        "int_columns": [],
        "defaults": {},
    },
    "menu_history": {
        "columns": ["id", "client_name", "service_date", "slot", "item_base", "created_at"],
        "pk": "id",
        "auto_id": True,
        "json_columns": [],
        "int_columns": ["id"],
        "defaults": {},
    },
    "week_signatures": {
        "columns": ["id", "client_name", "week_start", "week_signature", "created_at"],
        "pk": "id",
        "auto_id": True,
        "json_columns": [],
        "int_columns": ["id"],
        "defaults": {},
    },
    "users": {
        "columns": ["email", "profile_name", "password_hash", "role", "created_at"],
        "pk": "email",
        "json_columns": [],
        "int_columns": [],
        "defaults": {},
    },
}


# ---------------------------------------------------------------------------
# Result wrapper
# ---------------------------------------------------------------------------

class _Result:
    __slots__ = ("data",)

    def __init__(self, data: Any) -> None:
        self.data = data


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------

class _QueryBuilder:
    """Chainable builder; resolves against an Excel file on execute()."""

    def __init__(self, db: "ExcelDatabase", table: str) -> None:
        self._db = db
        self._table = table
        self._op: Optional[str] = None
        self._select_cols: Optional[str] = None
        self._payload: Any = None
        self._filters: List[tuple] = []
        self._order_col: Optional[str] = None
        self._limit_n: Optional[int] = None
        self._single: bool = False

    # ---- operation setters -------------------------------------------------

    def select(self, cols: str = "*") -> "_QueryBuilder":
        self._op = "select"
        self._select_cols = cols
        return self

    def insert(self, data: Any) -> "_QueryBuilder":
        self._op = "insert"
        self._payload = data
        return self

    def update(self, data: dict) -> "_QueryBuilder":
        self._op = "update"
        self._payload = data
        return self

    def delete(self) -> "_QueryBuilder":
        self._op = "delete"
        return self

    def upsert(self, data: Any) -> "_QueryBuilder":
        self._op = "upsert"
        self._payload = data
        return self

    # ---- filters -----------------------------------------------------------

    def eq(self, col: str, val: Any) -> "_QueryBuilder":
        self._filters.append((col, "eq", val))
        return self

    def in_(self, col: str, values: list) -> "_QueryBuilder":
        self._filters.append((col, "in", values))
        return self

    def gte(self, col: str, val: Any) -> "_QueryBuilder":
        self._filters.append((col, "gte", val))
        return self

    # ---- modifiers ---------------------------------------------------------

    def order(self, col: str) -> "_QueryBuilder":
        self._order_col = col
        return self

    def limit(self, n: int) -> "_QueryBuilder":
        self._limit_n = n
        return self

    def maybe_single(self) -> "_QueryBuilder":
        self._single = True
        return self

    # ---- execution ---------------------------------------------------------

    def execute(self) -> _Result:
        with self._db._lock:
            df = self._db._read(self._table)
            if self._op == "select":
                return self._do_select(df)
            if self._op == "insert":
                return self._do_insert(df)
            if self._op == "update":
                return self._do_update(df)
            if self._op == "delete":
                return self._do_delete(df)
            if self._op == "upsert":
                return self._do_upsert(df)
        return _Result(data=None)

    # ---- private -----------------------------------------------------------

    def _apply_filters(self, df: pd.DataFrame) -> pd.DataFrame:
        for col, op, val in self._filters:
            if col not in df.columns:
                continue
            str_series = df[col].astype(str)
            if op == "eq":
                mask = str_series == str(val)
            elif op == "in":
                str_vals = {str(v) for v in val}
                mask = str_series.isin(str_vals)
            elif op == "gte":
                # Exclude None rows before lexicographic date comparison
                mask = df[col].notna() & (str_series >= str(val))
            else:
                continue
            df = df[mask]
        return df

    def _parse_cols(self) -> Optional[List[str]]:
        if not self._select_cols or self._select_cols.strip() == "*":
            return None
        return [c.strip() for c in self._select_cols.split(",")]

    def _do_select(self, df: pd.DataFrame) -> _Result:
        df = self._apply_filters(df)
        if self._order_col and self._order_col in df.columns:
            df = df.sort_values(self._order_col)
        if self._limit_n is not None:
            df = df.head(self._limit_n)
        cols = self._parse_cols()
        if cols:
            existing = [c for c in cols if c in df.columns]
            df = df[existing] if existing else df
        rows = df.to_dict("records")
        if self._single:
            return _Result(data=rows[0] if rows else None)
        return _Result(data=rows)

    def _do_insert(self, df: pd.DataFrame) -> _Result:
        data = self._payload
        if isinstance(data, dict):
            data = [data]
        schema = _TABLES.get(self._table, {})
        new_rows: List[dict] = []
        for row in data:
            row = dict(row)
            if schema.get("auto_id") and "id" not in row:
                cur_max = int(df["id"].max()) if ("id" in df.columns and len(df) > 0) else 0
                row["id"] = cur_max + len(new_rows) + 1
            for col, default in schema.get("defaults", {}).items():
                if col not in row:
                    row[col] = default
            if "created_at" in schema.get("columns", []) and "created_at" not in row:
                row["created_at"] = dt.datetime.now().isoformat()
            new_rows.append(row)
        combined = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
        self._db._write(self._table, combined)
        return _Result(data=new_rows)

    def _do_update(self, df: pd.DataFrame) -> _Result:
        filtered = self._apply_filters(df)
        idx = filtered.index
        for col, val in self._payload.items():
            df.loc[idx, col] = val
        updated = df.loc[idx].to_dict("records")
        self._db._write(self._table, df)
        return _Result(data=updated)

    def _do_delete(self, df: pd.DataFrame) -> _Result:
        filtered = self._apply_filters(df)
        idx = filtered.index
        deleted = df.loc[idx].to_dict("records")
        df = df.drop(index=idx).reset_index(drop=True)
        self._db._write(self._table, df)
        return _Result(data=deleted)

    def _do_upsert(self, df: pd.DataFrame) -> _Result:
        data = self._payload
        if isinstance(data, dict):
            data = [data]
        schema = _TABLES.get(self._table, {})
        pk = schema.get("pk")
        for row in data:
            row = dict(row)
            if pk and pk in row and pk in df.columns:
                mask = df[pk].astype(str) == str(row[pk])
                if mask.any():
                    for col, val in row.items():
                        if col in df.columns:
                            df.loc[mask, col] = val
                    continue
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        self._db._write(self._table, df)
        return _Result(data=data)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class ExcelDatabase:
    """Process-wide Excel-backed database returned by get_supabase()."""

    def __init__(self, db_dir: str) -> None:
        self._dir = db_dir
        self._lock = threading.Lock()
        os.makedirs(db_dir, exist_ok=True)

    def table(self, name: str) -> _QueryBuilder:
        return _QueryBuilder(self, name)

    # ---- I/O ---------------------------------------------------------------

    def _path(self, table: str) -> str:
        return os.path.join(self._dir, f"{table}.xlsx")

    def _read(self, table: str) -> pd.DataFrame:
        path = self._path(table)
        if not os.path.exists(path):
            self._create_empty(table, path)
        df = pd.read_excel(path, dtype=str)
        # Replace NaN with None so callers get None for missing cells
        df = df.where(pd.notna(df), None)
        df = self._deserialize(table, df)
        df = self._coerce_ints(table, df)
        return df

    def _write(self, table: str, df: pd.DataFrame) -> None:
        serialized = self._serialize(table, df.copy())
        serialized.to_excel(self._path(table), index=False)

    def _create_empty(self, table: str, path: str) -> None:
        schema = _TABLES.get(table, {})
        pd.DataFrame(columns=schema.get("columns", [])).to_excel(path, index=False)
        logger.info("Created empty table: %s", path)

    def _serialize(self, table: str, df: pd.DataFrame) -> pd.DataFrame:
        schema = _TABLES.get(table, {})
        for col in schema.get("json_columns", []):
            if col in df.columns:
                df[col] = df[col].apply(
                    lambda v: json.dumps(v) if not isinstance(v, str) and v is not None else v
                )
        return df

    def _deserialize(self, table: str, df: pd.DataFrame) -> pd.DataFrame:
        schema = _TABLES.get(table, {})
        for col in schema.get("json_columns", []):
            if col in df.columns:
                def _parse(v, _col=col):
                    if v is None:
                        return v
                    if isinstance(v, str):
                        try:
                            return json.loads(v)
                        except (json.JSONDecodeError, ValueError):
                            return v
                    return v
                df[col] = df[col].apply(_parse)
        return df

    def _coerce_ints(self, table: str, df: pd.DataFrame) -> pd.DataFrame:
        schema = _TABLES.get(table, {})
        for col in schema.get("int_columns", []):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
        return df
