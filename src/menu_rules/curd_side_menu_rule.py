"""
Curd-side menu rule: biryani -> raita, pulao -> raita, else -> curd.
"""

from typing import Dict, Any, Set
from ortools.sat.python import cp_model
from .base_menu_rule import BaseMenuRule, MenuRuleType
from ..preprocessor.column_mapper import _norm_str
from src.constants import PULAO_SUBCATS


class CurdSideMenuRule(BaseMenuRule):
    """
    Config:
    {
        "type": "curd_side",
        "name": "curd_raita_logic",
        "pulao_subcats": ["south_veg_pulao", "north_simple_veg_pulao", ...]
    }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.CURD_SIDE
        self.pulao_subcats: Set[str] = set(rule_config.get('pulao_subcats', PULAO_SUBCATS))

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        cells = context.get('cells', [])
        dates = context.get('dates', [])
        day_types = context.get('day_types', [])
        find_cells = context.get('find_cells_fn')
        link_any = context.get('link_any_fn')

        if not cells or not find_cells or not link_any:
            return

        for di, _ in enumerate(dates):
            day_type = day_types[di] if di < len(day_types) else 'normal'
            rice_cells = find_cells(cells, di, 'rice')
            curd_cells = find_cells(cells, di, 'curd_side')

            if not rice_cells or not curd_cells:
                continue

            # Pulao detection in rice
            rice_pulao_lits = [
                v for rc in rice_cells
                for v, row in zip(rc.x_vars, rc.cand_rows)
                if _norm_str(row.get('sub_category', '')) in self.pulao_subcats
            ]
            rice_is_pulao = model.NewBoolVar(f'rice_is_pulao_{di}')
            link_any(model, rice_pulao_lits, rice_is_pulao)

            # Curd vs raita detection
            curd_lits, raita_lits = [], []
            for cc in curd_cells:
                for v, row in zip(cc.x_vars, cc.cand_rows):
                    sc = _norm_str(row.get('sub_category', ''))
                    if sc == 'curd':
                        curd_lits.append(v)
                    if int(row.get('is_raita', 0)) == 1 or 'raita' in sc:
                        raita_lits.append(v)

            curd_is_curd = model.NewBoolVar(f'curd_is_curd_{di}')
            link_any(model, curd_lits, curd_is_curd)
            curd_is_raita = model.NewBoolVar(f'curd_is_raita_{di}')
            link_any(model, raita_lits, curd_is_raita)

            if day_type == 'biryani':
                model.Add(curd_is_raita == 1)
            else:
                model.Add(curd_is_raita == 1).OnlyEnforceIf(rice_is_pulao)
                model.Add(curd_is_curd == 1).OnlyEnforceIf(rice_is_pulao.Not())
