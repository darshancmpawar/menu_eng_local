"""
Menu rule definitions and handlers for menu planning.
"""

from .base_menu_rule import (
    BaseMenuRule,
    MenuRuleType,
    MenuRuleSeverity,
    Diagnostic,
    DiagnosticSeverity,
    DiagnosticPhase,
    DiagnoseContext,
)
from .diagnostics import (
    run_diagnostics,
    summarize,
    has_blocking_errors,
    pool_warnings_projection,
)
from .cuisine_menu_rule import CuisineMenuRule
from .unique_items_menu_rule import UniqueItemsMenuRule
from .coupling_menu_rule import CouplingMenuRule
from .curd_side_menu_rule import CurdSideMenuRule
from .premium_menu_rule import PremiumMenuRule

# Consolidated domain modules
from .theme_rules import (
    ThemeDayMenuRule,
    ThemeSlotFilterRule,
    ThemeStarterPreferenceRule,
    ThemeFallbackPenaltyRule,
)
from .color_rules import (
    ColorPairingMenuRule,
    ColorVarietyMenuRule,
    WelcomeDrinkColorMenuRule,
)
from .cooldown_rules import (
    ItemCooldownMenuRule,
    RiceBreadGapMenuRule,
    WeekSignatureCooldownMenuRule,
)
from .nonveg_rules import (
    NonvegBiryaniWeeklyRule,
    NonvegDryPreferenceRule,
)

# Per-client rules
from .ingredient_ban_rule import IngredientBanRule
from .item_frequency_rule import ItemFrequencyRule
from .slot_day_restriction_rule import SlotDayRestrictionRule

from .menu_rule_loader import MenuRuleLoader

__all__ = [
    'BaseMenuRule', 'MenuRuleType', 'MenuRuleSeverity', 'MenuRuleLoader',
    # diagnostics
    'Diagnostic', 'DiagnosticSeverity', 'DiagnosticPhase', 'DiagnoseContext',
    'run_diagnostics', 'summarize', 'has_blocking_errors',
    'pool_warnings_projection',
    'CuisineMenuRule', 'UniqueItemsMenuRule', 'CouplingMenuRule',
    'CurdSideMenuRule', 'PremiumMenuRule',
    # theme
    'ThemeDayMenuRule', 'ThemeSlotFilterRule',
    'ThemeStarterPreferenceRule', 'ThemeFallbackPenaltyRule',
    # color
    'ColorPairingMenuRule', 'ColorVarietyMenuRule', 'WelcomeDrinkColorMenuRule',
    # cooldown
    'ItemCooldownMenuRule', 'RiceBreadGapMenuRule',
    'WeekSignatureCooldownMenuRule',
    # nonveg
    'NonvegBiryaniWeeklyRule', 'NonvegDryPreferenceRule',
    # per-client
    'IngredientBanRule', 'ItemFrequencyRule', 'SlotDayRestrictionRule',
]
