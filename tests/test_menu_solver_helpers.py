"""Tests for internal ``menu_solver`` helpers (cell lookup)."""

from src.solver.menu_solver import _find_cells, _make_find_cells


class _Stub:
    __slots__ = ("d_idx", "base_slot")

    def __init__(self, d_idx, base_slot):
        self.d_idx = d_idx
        self.base_slot = base_slot


def _sample_cells():
    return [
        _Stub(0, "bread"),
        _Stub(0, "rice"),
        _Stub(0, "rice"),
        _Stub(1, "bread"),
        _Stub(1, "starter"),
    ]


class TestLinearFindCells:
    def test_returns_matching_cells(self):
        cells = _sample_cells()
        out = _find_cells(cells, 0, "rice")
        assert [c.base_slot for c in out] == ["rice", "rice"]

    def test_returns_empty_when_no_match(self):
        assert _find_cells(_sample_cells(), 2, "bread") == []


class TestIndexedFindCells:
    def test_matches_linear_variant_for_all_keys(self):
        cells = _sample_cells()
        find = _make_find_cells(cells)
        for di in range(3):
            for slot in ("bread", "rice", "starter", "welcome_drink"):
                assert find(cells, di, slot) == _find_cells(cells, di, slot)

    def test_missing_key_returns_empty(self):
        find = _make_find_cells(_sample_cells())
        assert find([], 9, "nonexistent") == []

    def test_closure_ignores_first_arg(self):
        # The closure closes over cells at build time; the first arg is a
        # vestigial signature artifact kept for rule call-site compatibility.
        find = _make_find_cells(_sample_cells())
        assert find([], 0, "rice") == find(_sample_cells(), 0, "rice")
