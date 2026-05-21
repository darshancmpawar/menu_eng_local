"""
Data cleanser for menu data.

Handles deduplication, missing values, and text standardization
for the full Ontology schema (including flag columns).
"""

import logging

import pandas as pd

logger = logging.getLogger(__name__)


class DataCleanser:
    """
    Cleaning layer that runs *after* ColumnMapper has normalized the schema.
    Handles deduplication, empty-row removal, and final validation.
    """

    def __init__(self, data: pd.DataFrame):
        self.raw_data = data.copy()
        self.cleaned_data = None

    def clean(self) -> pd.DataFrame:
        """
        Perform cleaning operations on the data.

        Returns:
            Cleaned DataFrame.
        """
        df = self.raw_data.copy()

        # 1. Remove duplicates based on item_id (or item if item_id absent)
        dedup_col = 'item_id' if 'item_id' in df.columns else 'item'
        initial_count = len(df)
        df = df.drop_duplicates(subset=[dedup_col], keep='first')
        removed = initial_count - len(df)
        if removed > 0:
            logger.info("Removed %d duplicate items", removed)

        # 2. Fill missing text fields
        for col in ('item', 'item_name', 'course_type', 'cuisine_family', 'item_color',
                     'key_ingredient', 'sub_category'):
            if col in df.columns:
                df[col] = df[col].fillna('')

        # 3. Remove rows with empty item or course_type
        if 'item' in df.columns:
            df = df[df['item'].astype(str).str.strip().str.len() > 0]
        if 'course_type' in df.columns:
            df = df[df['course_type'].astype(str).str.strip().str.len() > 0]

        # 4. Ensure all flag columns are int (fill NaN with 0)
        flag_cols = [c for c in df.columns if c.startswith('is_')]
        for col in flag_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0).astype(int)

        self.cleaned_data = df
        logger.info("Cleaned data: %d items ready", len(df))
        return df
