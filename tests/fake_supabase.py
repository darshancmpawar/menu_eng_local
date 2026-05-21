"""
In-memory fake implementing the subset of the ``supabase`` client API that
the app uses: chained ``.table().select().eq().order().maybe_single().execute()``
plus ``.insert()``, ``.update()``, and ``.delete()``.

Not a full re-implementation — only the query shapes actually exercised by
the app are supported.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional


class _Response:
    __slots__ = ("data",)

    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows: List[Dict[str, Any]], table: "_Table"):
        self._rows = rows
        self._table = table
        self._filters: List[tuple] = []
        self._order: Optional[str] = None
        self._limit: Optional[int] = None
        self._single = False
        self._mode = "select"
        self._payload: Any = None

    # -- filters -----------------------------------------------------------

    def select(self, *_args, **_kwargs):
        self._mode = "select"
        return self

    def eq(self, col: str, val: Any):
        self._filters.append(("eq", col, val))
        return self

    def gte(self, col: str, val: Any):
        self._filters.append(("gte", col, val))
        return self

    def lte(self, col: str, val: Any):
        self._filters.append(("lte", col, val))
        return self

    def in_(self, col: str, values: Iterable[Any]):
        self._filters.append(("in", col, list(values)))
        return self

    def order(self, col: str, **_kwargs):
        self._order = col
        return self

    def limit(self, n: int):
        self._limit = int(n)
        return self

    def maybe_single(self):
        self._single = True
        return self

    # -- mutations ---------------------------------------------------------

    def insert(self, payload):
        self._mode = "insert"
        self._payload = payload
        return self

    def update(self, payload: Dict[str, Any]):
        self._mode = "update"
        self._payload = payload
        return self

    def delete(self):
        self._mode = "delete"
        return self

    # -- terminal ----------------------------------------------------------

    def _match(self, row: Dict[str, Any]) -> bool:
        for f in self._filters:
            op, col, val = f
            cell = row.get(col)
            if op == "eq":
                if cell != val:
                    return False
            elif op == "gte":
                if cell is None or cell < val:
                    return False
            elif op == "lte":
                if cell is None or cell > val:
                    return False
            elif op == "in":
                if cell not in val:
                    return False
            else:
                raise RuntimeError(f"Unsupported filter op: {op}")
        return True

    def execute(self) -> _Response:
        if self._mode == "select":
            rows = [copy.deepcopy(r) for r in self._rows if self._match(r)]
            if self._order:
                rows.sort(key=lambda r: (r.get(self._order) is None, r.get(self._order)))
            if self._limit is not None:
                rows = rows[: self._limit]
            if self._single:
                return _Response(rows[0] if rows else None)
            return _Response(rows)

        if self._mode == "insert":
            payload = self._payload
            new_rows = payload if isinstance(payload, list) else [payload]
            self._rows.extend(copy.deepcopy(r) for r in new_rows)
            return _Response([copy.deepcopy(r) for r in new_rows])

        if self._mode == "update":
            updated = []
            for row in self._rows:
                if self._match(row):
                    row.update(copy.deepcopy(self._payload))
                    updated.append(copy.deepcopy(row))
            return _Response(updated)

        if self._mode == "delete":
            kept = [r for r in self._rows if not self._match(r)]
            removed = [r for r in self._rows if self._match(r)]
            self._rows.clear()
            self._rows.extend(kept)
            return _Response([copy.deepcopy(r) for r in removed])

        raise RuntimeError(f"Unknown query mode: {self._mode}")


class _Table:
    def __init__(self, name: str, store: "FakeSupabase"):
        self.name = name
        self._store = store

    def select(self, *args, **kwargs):
        return self._new_query().select(*args, **kwargs)

    def insert(self, payload):
        return self._new_query().insert(payload)

    def update(self, payload):
        return self._new_query().update(payload)

    def delete(self):
        return self._new_query().delete()

    def _new_query(self) -> _Query:
        return _Query(self._store._rows(self.name), self)


class FakeSupabase:
    """A minimal in-memory stand-in for ``supabase.Client``."""

    def __init__(self, seed: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self._tables: Dict[str, List[Dict[str, Any]]] = {}
        if seed:
            for name, rows in seed.items():
                self._tables[name] = [copy.deepcopy(r) for r in rows]

    def _rows(self, name: str) -> List[Dict[str, Any]]:
        return self._tables.setdefault(name, [])

    def table(self, name: str) -> _Table:
        return _Table(name, self)

    # Test helpers ---------------------------------------------------------

    def seed(self, name: str, rows: Iterable[Dict[str, Any]]):
        self._tables.setdefault(name, []).extend(copy.deepcopy(r) for r in rows)

    def rows(self, name: str) -> List[Dict[str, Any]]:
        return self._tables.get(name, [])
