"""
Column mapper for Ontology Excel files.

Auto-detects column aliases and normalizes flag columns to match
the canonical schema expected by the solver pipeline.
"""

import pandas as pd
from typing import Dict, List, Optional


# --- Normalization helpers (match old menu_eng_latest_v27.py) ---

def _norm_str(x) -> str:
    if pd.isna(x):
        return ''
    return str(x).strip().lower()


def _norm_color(x) -> str:
    s = _norm_str(x).replace(' ', '_')
    return 'unknown' if s in ('', 'na', 'nan', 'null', 'none', 'unknown', 'unk') else s


def _to_bool01(x) -> int:
    if pd.isna(x):
        return 0
    if isinstance(x, (int, float)):
        return int(x != 0)
    return 1 if str(x).strip().lower() in ('1', 'y', 'yes', 'true', 't') else 0


def pick_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    """Return the first matching column name (case-insensitive) from *candidates*."""
    cols = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in cols:
            return cols[cand.lower()]
    return None


# --- Required column aliases ---

REQUIRED_COL_ALIASES: Dict[str, List[str]] = {
    'item': ['item', 'menu_items', 'menu_item'],
    'course_type': ['course_type', 'course', 'slot'],
    'cuisine_family': ['cuisine_family', 'cuisine_family_', 'cuisine_family_region', 'cuisine'],
    'item_color': ['item_color', 'colour', 'color', 'color_group', 'dominant_color'],
    'key_ingredient': ['key_ingredient', 'keyingredient', 'ingredient_key'],
    'sub_category': ['sub_category', 'subcategory'],
}

# --- Optional flag column aliases ---

OPTIONAL_FLAG_ALIASES: Dict[str, List[str]] = {
    'is_liquid_rice': ['is_liquid_rice'],
    'is_rice_bread': ['is_rice_bread'],
    'is_deep_fried_veg_dry': ['is_deep_fried_veg_dry'],
    'is_chinese_fried_rice': ['is_chinese_fried_rice'],
    'is_chinese_chicken_gravy': ['is_chinese_chicken_gravy'],
    'is_chinese_veg_gravy': ['is_chinese_veg_gravy'],
    'is_chinese_starter': ['is_chinese_starter'],
    'is_nonveg_biryani': ['is_nonveg_biryani'],
    'is_mixedveg_biryani': ['is_mixedveg_biryani'],
    'is_raita': ['is_raita'],
    'is_premium_veg': ['is_premium_veg'],
    'is_deep_fried_starter': ['is_deep_fried_starter'],
    'is_nonveg_dry': ['is_nonveg_dry', 'is_non_veg_dry', 'nonveg_dry', 'non_veg_dry'],
    'is_nonveg_gravy': ['is_nonveg_gravy', 'is_non_veg_gravy', 'nonveg_gravy', 'non_veg_gravy'],
}

# Deep-fried starter detection keywords
DEEPFRIED_STARTER_HINT_WORDS = (
    'fried', 'fry', 'pakoda', 'pakora', 'vada', 'bonda', 'bhaji', 'bajji', 'cutlet',
)


class ColumnMapper:
    """
    Maps raw Ontology Excel columns to canonical names, resolves aliases,
    and normalizes flag columns.
    """

    def __init__(self):
        self.rename_map: Dict[str, str] = {}
        self.missing_required: List[str] = []

    def detect(self, df: pd.DataFrame) -> 'ColumnMapper':
        """Detect column aliases and build the rename map. Returns self for chaining."""
        self.rename_map = {}
        self.missing_required = []

        # Required columns: item and course_type must exist
        for canon, aliases in REQUIRED_COL_ALIASES.items():
            found = pick_col(df, aliases)
            if found is not None and found != canon:
                self.rename_map[found] = canon
            elif found is None:
                if canon in ('item', 'course_type'):
                    self.missing_required.append(canon)
                # Optional required cols get default values in apply()

        # Optional flag columns: resolve aliases
        for canon, aliases in OPTIONAL_FLAG_ALIASES.items():
            found = pick_col(df, aliases)
            if found is not None and found != canon and canon not in df.columns:
                self.rename_map[found] = canon

        return self

    def validate(self) -> Dict:
        """Return validation result after detect()."""
        if self.missing_required:
            return {
                'valid': False,
                'missing_columns': self.missing_required,
                'error': f"Missing required columns: {self.missing_required}",
            }
        return {'valid': True}

    def apply(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Apply column renaming, add missing optional columns with defaults,
        normalize text and flag columns, generate item_id, compute key_eff.
        """
        df = df.rename(columns=self.rename_map)

        # Ensure optional text columns exist
        if 'cuisine_family' not in df.columns:
            df['cuisine_family'] = ''
        if 'item_color' not in df.columns:
            df['item_color'] = 'unknown'
        if 'key_ingredient' not in df.columns:
            df['key_ingredient'] = ''
        if 'sub_category' not in df.columns:
            df['sub_category'] = ''

        # Generate item_id from item name if not present
        if 'item_id' not in df.columns:
            df['item_id'] = df['item'].astype(str).str.strip()

        # Normalize text columns
        for col, fn in (
            ('item', _norm_str),
            ('course_type', _norm_str),
            ('cuisine_family', _norm_str),
            ('item_color', _norm_color),
            ('key_ingredient', _norm_str),
            ('sub_category', _norm_str),
        ):
            if col in df.columns:
                df[col] = df[col].map(fn)

        # Normalize flag columns
        all_flag_cols = list(OPTIONAL_FLAG_ALIASES.keys())
        for col in all_flag_cols:
            if col in df.columns:
                df[col] = df[col].map(_to_bool01)
            else:
                df[col] = 0

        # Detect deep-fried starters via heuristic
        df['is_deep_fried_starter'] = df.apply(_is_deepfried_starter_row, axis=1).astype(int)

        # Detect nonveg dry via heuristic
        df['is_nonveg_dry'] = df.apply(_is_nonveg_dry_row, axis=1).astype(int)

        # Compute key_eff (effective key ingredient for uniqueness)
        cat_series = df['category'].map(_norm_str) if 'category' in df.columns else pd.Series(
            [''] * len(df), index=df.index
        )
        df['key_eff'] = df.apply(
            lambda r: _compute_key_eff(r, cat_series), axis=1
        ).map(_norm_str)

        return df


def _is_deepfried_starter_row(row: pd.Series) -> bool:
    if 'is_deep_fried_starter' in row.index and int(row.get('is_deep_fried_starter', 0)) == 1:
        return True
    text = f"{_norm_str(row.get('item', ''))} {_norm_str(row.get('sub_category', ''))}"
    return any(w in text for w in DEEPFRIED_STARTER_HINT_WORDS)


def _is_nonveg_dry_row(row: pd.Series) -> bool:
    if int(_to_bool01(row.get('is_nonveg_dry', 0))) == 1:
        return True
    if _norm_str(row.get('category', '')) == 'chicken_dry':
        return True
    text = ' '.join((
        _norm_str(row.get('sub_category', '')),
        _norm_str(row.get('key_ingredient', '')),
        _norm_str(row.get('item', '')),
    ))
    return ('chicken_dry' in text) or ('chicken dry' in text)


def _compute_key_eff(row: pd.Series, cat_series: pd.Series) -> str:
    k = row['key_ingredient'] if row['key_ingredient'] else row['item']
    cf = row['cuisine_family']
    sc = row['sub_category']
    ct = row['course_type']
    cat = cat_series.loc[row.name] if row.name in cat_series.index else ''
    if k in ('chicken', 'egg') or ct in ('rice', 'healthy_rice') or cat.startswith('flavoured_rice') or ('biryani' in cat):
        return '_'.join(p for p in (k, cf, sc) if p)
    return k
