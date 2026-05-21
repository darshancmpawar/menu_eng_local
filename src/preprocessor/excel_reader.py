"""
Excel file reader for menu data.

Reads Ontology.xlsx (or any menu-items Excel) and applies ColumnMapper
to resolve aliases and normalise the schema before returning a DataFrame.
"""

import logging
from pathlib import Path
from typing import Dict, Any, Union

import pandas as pd

logger = logging.getLogger(__name__)

from .column_mapper import ColumnMapper


class ExcelReader:
    """
    Reads menu data from Excel files and validates/normalizes the schema
    via ColumnMapper.
    """

    def __init__(self, file_path: str, sheet_name: Union[str, int] = 0):
        self.file_path = Path(file_path)
        self.sheet_name = sheet_name
        self.data = None
        self._mapper = ColumnMapper()

    def read(self) -> pd.DataFrame:
        """
        Read the Excel file, apply column mapping & normalization.

        Returns:
            pandas DataFrame with canonical column names and normalized values.
        """
        if not self.file_path.exists():
            raise FileNotFoundError(f"Excel file not found: {self.file_path}")

        raw = pd.read_excel(self.file_path, sheet_name=self.sheet_name)

        # Detect aliases and validate
        self._mapper.detect(raw)
        validation = self._mapper.validate()
        if not validation['valid']:
            raise ValueError(validation['error'])

        # Apply renaming, normalization, flag handling, key_eff computation
        self.data = self._mapper.apply(raw)

        logger.info("Read %d menu items from %s", len(self.data), self.file_path)
        return self.data

    def validate_schema(self) -> Dict[str, Any]:
        """
        Validate that the loaded data has the required columns.

        Returns:
            Dictionary with validation results.
        """
        if self.data is None:
            return {'valid': False, 'error': 'No data loaded'}

        required = ['item', 'course_type']
        missing = [c for c in required if c not in self.data.columns]
        if missing:
            return {
                'valid': False,
                'missing_columns': missing,
                'error': f"Missing required columns: {missing}",
            }

        return {
            'valid': True,
            'columns': list(self.data.columns),
            'row_count': len(self.data),
        }
