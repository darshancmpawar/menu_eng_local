"""
Menu rule loader from JSON configuration.
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

from .base_menu_rule import BaseMenuRule
from .cuisine_menu_rule import CuisineMenuRule
from .unique_items_menu_rule import UniqueItemsMenuRule
from .coupling_menu_rule import CouplingMenuRule
from .curd_side_menu_rule import CurdSideMenuRule
from .premium_menu_rule import PremiumMenuRule
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
from .ingredient_ban_rule import IngredientBanRule
from .item_frequency_rule import ItemFrequencyRule
from .slot_day_restriction_rule import SlotDayRestrictionRule


def _log_invalid_rule(
    rule: Optional[BaseMenuRule],
    rule_config: Dict[str, Any],
    *,
    scope: str,
) -> None:
    """Log an invalid rule with its name, type, and any reasons provided
    by the rule's ``validation_errors()`` hook. A generic "invalid" message
    stranded admins with no way to know which field was wrong.
    """
    name = rule_config.get('name') or (rule.name if rule else '<unnamed>')
    rule_type = rule_config.get('type', '?')
    errs = rule.validation_errors() if rule is not None else []
    if errs:
        logger.warning(
            "Skipping invalid %s '%s' (type=%s): %s",
            scope, name, rule_type, "; ".join(errs),
        )
    else:
        logger.warning(
            "Skipping invalid %s '%s' (type=%s): validate_config() returned False",
            scope, name, rule_type,
        )


class MenuRuleLoader:
    """Loads menu rules from JSON configuration files."""

    RULE_CLASSES = {
        'cuisine': CuisineMenuRule,
        'color_pairing': ColorPairingMenuRule,
        'color_variety': ColorVarietyMenuRule,
        'unique_items': UniqueItemsMenuRule,
        'theme_day': ThemeDayMenuRule,
        'coupling': CouplingMenuRule,
        'curd_side': CurdSideMenuRule,
        'premium': PremiumMenuRule,
        'welcome_drink_color': WelcomeDrinkColorMenuRule,
        'week_signature_cooldown': WeekSignatureCooldownMenuRule,
        'theme_starter_preference': ThemeStarterPreferenceRule,
        'theme_fallback_penalty': ThemeFallbackPenaltyRule,
        'item_cooldown': ItemCooldownMenuRule,
        'ricebread_gap': RiceBreadGapMenuRule,
        'theme_slot_filter': ThemeSlotFilterRule,
        'nonveg_dry_preference': NonvegDryPreferenceRule,
        'nonveg_biryani_weekly': NonvegBiryaniWeeklyRule,
        'ingredient_ban': IngredientBanRule,
        'item_frequency': ItemFrequencyRule,
        'slot_day_restriction': SlotDayRestrictionRule,
    }

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path) if config_path else None
        self.rules = []

    def load_from_file(self, config_path: str = None) -> List[BaseMenuRule]:
        if config_path:
            self.config_path = Path(config_path)
        if not self.config_path or not self.config_path.exists():
            raise FileNotFoundError(f"Menu rule config file not found: {self.config_path}")
        with open(self.config_path, 'r') as f:
            config_data = json.load(f)
        return self.load_from_dict(config_data)

    def load_from_dict(self, config_data: Dict[str, Any]) -> List[BaseMenuRule]:
        self.rules = []
        rules_list = config_data.get('rules', config_data.get('constraints', []))
        for rule_config in rules_list:
            try:
                rule = self._create_rule(rule_config)
                if rule and rule.validate_config():
                    self.rules.append(rule)
                else:
                    _log_invalid_rule(rule, rule_config, scope="rule")
            except (ValueError, KeyError, TypeError) as e:
                logger.warning("Error creating rule: %s", e)
        logger.info("Loaded %d menu rule(s)", len(self.rules))
        return self.rules

    def _create_rule(self, rule_config: Dict[str, Any]) -> Optional[BaseMenuRule]:
        rule_type = rule_config.get('type', '').lower()
        if rule_type not in self.RULE_CLASSES:
            raise ValueError(f"Unknown rule type: {rule_type}")
        return self.RULE_CLASSES[rule_type](rule_config)

    def load_for_client(
        self, client_name: str, generic_rules: List[BaseMenuRule],
    ) -> List[BaseMenuRule]:
        """Return *generic_rules* plus any per-client rules for *client_name*.

        Reads ``CLIENT_RULES_CONFIG_PATH`` fresh every call.  If the file is
        missing or the client has no entry, returns *generic_rules* unchanged.
        """
        from api.config import CLIENT_RULES_CONFIG_PATH

        path = Path(CLIENT_RULES_CONFIG_PATH)
        if not path.exists():
            return list(generic_rules)
        try:
            with open(path, 'r') as f:
                blob = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read client_rules.json: %s", exc)
            return list(generic_rules)

        client_block = blob.get(client_name)
        if not client_block:
            return list(generic_rules)

        extra: List[BaseMenuRule] = []
        for rule_cfg in client_block:
            try:
                rule = self._create_rule(rule_cfg)
                if rule and rule.validate_config():
                    extra.append(rule)
                else:
                    _log_invalid_rule(
                        rule, rule_cfg,
                        scope=f"per-client rule for {client_name}",
                    )
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning(
                    "Error creating per-client rule for %s: %s",
                    client_name, exc)
        logger.info(
            "Loaded %d extra rule(s) for client '%s'",
            len(extra), client_name)
        return list(generic_rules) + extra

    def get_rules_by_type(self, rule_type: str) -> List[BaseMenuRule]:
        return [r for r in self.rules if r.rule_type.value == rule_type]

    def get_enabled_rules(self) -> List[BaseMenuRule]:
        return list(self.rules)
