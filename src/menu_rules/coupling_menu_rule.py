"""
Coupling menu rule: bread-rice-starter-vegdry deep-fried coupling.

rice-bread bread <=> liquid rice <=> deep-fried starter <=> deep-fried veg_dry.
Max 1 rice-bread day per week, max 1 deep-fried veg_dry day per week.
"""

from typing import Dict, Any, List
from ortools.sat.python import cp_model
from .base_menu_rule import (
    BaseMenuRule,
    Diagnostic,
    DiagnosticPhase,
    DiagnosticSeverity,
    DiagnoseContext,
    MenuRuleType,
)


class CouplingMenuRule(BaseMenuRule):
    """
    Config:
    {
        "type": "coupling",
        "name": "deep_fried_coupling"
    }
    """

    def __init__(self, rule_config: Dict[str, Any]):
        super().__init__(rule_config)
        self.rule_type = MenuRuleType.COUPLING

    def apply(self, model: cp_model.CpModel, variables: Dict[str, Any],
              menu_data: Any, context: Dict[str, Any]) -> None:
        cells = context.get('cells', [])
        dates = context.get('dates', [])
        find_cells = context.get('find_cells_fn')
        link_any = context.get('link_any_fn')

        if not cells or not find_cells or not link_any:
            return

        bread_rb_day: List = []
        vegdry_df_day: List = []

        for di in range(len(dates)):
            bread_cells = find_cells(cells, di, 'bread')
            rice_cells = find_cells(cells, di, 'rice')
            starter_cells = find_cells(cells, di, 'starter')
            vegdry_cells = find_cells(cells, di, 'veg_dry')

            if not bread_cells or not rice_cells or not starter_cells or not vegdry_cells:
                continue

            # Rice-bread detection
            bread_rb_lits = [
                v for c in bread_cells
                for v, r in zip(c.x_vars, c.cand_rows)
                if int(r.get('is_rice_bread', 0)) == 1
            ]
            bread_rb = model.NewBoolVar(f'bread_ricebread_{di}')
            link_any(model, bread_rb_lits, bread_rb)
            bread_rb_day.append(bread_rb)

            # Liquid rice detection
            rice_liq_lits = [
                v for c in rice_cells
                for v, r in zip(c.x_vars, c.cand_rows)
                if int(r.get('is_liquid_rice', 0)) == 1
            ]
            rice_liq = model.NewBoolVar(f'rice_liquid_{di}')
            link_any(model, rice_liq_lits, rice_liq)

            # Deep-fried starter detection — reads the column populated by
            # ColumnMapper.apply() rather than re-running the heuristic.
            starter_df_lits = [
                v for c in starter_cells
                for v, r in zip(c.x_vars, c.cand_rows)
                if int(r.get('is_deep_fried_starter', 0)) == 1
            ]
            starter_df = model.NewBoolVar(f'starter_deepfried_{di}')
            link_any(model, starter_df_lits, starter_df)

            # Deep-fried veg_dry detection
            vegdry_df_vars = []
            for idx, vc in enumerate(vegdry_cells, start=1):
                df_lits = [
                    v for v, r in zip(vc.x_vars, vc.cand_rows)
                    if int(r.get('is_deep_fried_veg_dry', 0)) == 1
                ]
                vdf = model.NewBoolVar(f'vegdry_deepfried_{di}_{idx}')
                link_any(model, df_lits, vdf)
                vegdry_df_vars.append(vdf)
            vegdry_any = model.NewBoolVar(f'vegdry_any_deepfried_{di}')
            if vegdry_df_vars:
                model.AddMaxEquality(vegdry_any, vegdry_df_vars)
            else:
                model.Add(vegdry_any == 0)
            vegdry_df_day.append(vegdry_any)

            # Coupling constraints
            model.Add(bread_rb <= rice_liq)
            model.Add(bread_rb <= starter_df)
            # Prefer coupling deep-fried starters with rice-bread only when the
            # paired rice/bread pattern is actually available in this day's pools.
            # This keeps the legacy coupling behavior in normal cases while
            # avoiding hard infeasibility when themed filters leave no liquid-rice
            # or rice-bread candidates.
            if bread_rb_lits and rice_liq_lits:
                model.Add(starter_df <= bread_rb)
            model.Add(vegdry_any <= rice_liq)
            model.Add(vegdry_any <= bread_rb)

        # Weekly limits
        if bread_rb_day:
            model.Add(sum(bread_rb_day) <= 1)
        if vegdry_df_day:
            model.Add(sum(vegdry_df_day) <= 1)

    def diagnose(self, ctx: DiagnoseContext) -> List[Diagnostic]:
        """Coupling constraints are upper bounds (rice-bread ⇒ liquid
        rice, etc.) so they rarely cause hard infeasibility on their
        own. The diagnostic value here is calling out **asymmetric
        data**: a client whose bread pool has rice-bread items but
        whose rice pool has NO liquid-rice items will silently never
        pick those rice-breads — and the user's intent ("I added these
        rice-breads so they'd appear") is lost.

        Emits WARNING per asymmetric pair so the user can either add
        the missing pair or remove the orphaned data.
        """
        diags: List[Diagnostic] = []
        bread = ctx.pools.get('bread')
        rice = ctx.pools.get('rice')
        starter = ctx.pools.get('starter')

        def _flag_count(pool, col):
            if pool is None or col not in pool.columns:
                return None
            return int(pool[col].fillna(0).astype(int).eq(1).sum())

        rb = _flag_count(bread, 'is_rice_bread')
        liquid = _flag_count(rice, 'is_liquid_rice')
        fried_starter = _flag_count(starter, 'is_deep_fried_starter')

        if rb and liquid == 0:
            diags.append(Diagnostic(
                rule=self.name, rule_type=self.rule_type.value,
                severity=DiagnosticSeverity.WARNING,
                phase=DiagnosticPhase.APPLY,
                message=(
                    f"Bread pool has {rb} rice-bread item"
                    f"{'s' if rb != 1 else ''} but rice pool has 0 "
                    f"liquid-rice items. The coupling constraint "
                    f"forbids picking rice-bread without liquid rice, "
                    f"so those rice-breads will never be selected."
                ),
                suggestion=(
                    "Add at least one liquid-rice item (is_liquid_rice=1) "
                    "to the rice pool, or remove the rice-bread items if "
                    "the asymmetry is intentional."
                ),
                affected={
                    'is_rice_bread_count': rb,
                    'is_liquid_rice_count': liquid,
                },
            ))

        if rb and fried_starter == 0 and starter is not None:
            diags.append(Diagnostic(
                rule=self.name, rule_type=self.rule_type.value,
                severity=DiagnosticSeverity.WARNING,
                phase=DiagnosticPhase.APPLY,
                message=(
                    f"Bread pool has {rb} rice-bread item"
                    f"{'s' if rb != 1 else ''} but starter pool has 0 "
                    f"deep-fried starters. The coupling constraint "
                    f"forbids picking rice-bread without a deep-fried "
                    f"starter, so those rice-breads will never be "
                    f"selected."
                ),
                suggestion=(
                    "Add at least one deep-fried starter "
                    "(is_deep_fried_starter=1) to the starter pool, or "
                    "remove the rice-bread items."
                ),
                affected={
                    'is_rice_bread_count': rb,
                    'is_deep_fried_starter_count': fried_starter,
                },
            ))
        return diags
