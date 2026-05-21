"""Tests for MenuRuleLoader and BaseMenuRule."""

import pytest
from src.menu_rules import MenuRuleLoader
from src.menu_rules.base_menu_rule import BaseMenuRule, MenuRuleType


class TestMenuRuleLoader:
    def test_load_from_json_file(self):
        loader = MenuRuleLoader('data/configs/indian_menu_rules.json')
        rules = loader.load_from_file()
        assert len(rules) == 14

    def test_all_rules_are_base_menu_rule(self):
        loader = MenuRuleLoader('data/configs/indian_menu_rules.json')
        rules = loader.load_from_file()
        for rule in rules:
            assert isinstance(rule, BaseMenuRule)

    def test_all_rules_have_rule_type(self):
        loader = MenuRuleLoader('data/configs/indian_menu_rules.json')
        rules = loader.load_from_file()
        for rule in rules:
            assert rule.rule_type is not None
            assert isinstance(rule.rule_type, MenuRuleType)

    def test_all_rules_validate(self):
        loader = MenuRuleLoader('data/configs/indian_menu_rules.json')
        rules = loader.load_from_file()
        for rule in rules:
            assert rule.validate_config() is True

    def test_load_from_dict(self):
        config = {
            "rules": [
                {"name": "test_premium", "type": "premium", "max_per_day": 1,
                 "min_per_horizon": 1, "max_per_horizon": 2}
            ]
        }
        loader = MenuRuleLoader()
        rules = loader.load_from_dict(config)
        assert len(rules) == 1
        assert rules[0].name == "test_premium"
        assert rules[0].rule_type == MenuRuleType.PREMIUM

    def test_unknown_rule_type_skipped(self):
        config = {"rules": [{"name": "bad", "type": "nonexistent"}]}
        loader = MenuRuleLoader()
        rules = loader.load_from_dict(config)
        assert len(rules) == 0

    def test_get_rules_by_type(self):
        loader = MenuRuleLoader('data/configs/indian_menu_rules.json')
        loader.load_from_file()
        premiums = loader.get_rules_by_type('premium')
        assert len(premiums) == 1

    def test_get_enabled_rules_returns_all(self):
        loader = MenuRuleLoader('data/configs/indian_menu_rules.json')
        rules = loader.load_from_file()
        enabled = loader.get_enabled_rules()
        assert len(enabled) == len(rules)

    def test_missing_file_raises(self):
        loader = MenuRuleLoader('/nonexistent/file.json')
        with pytest.raises(FileNotFoundError):
            loader.load_from_file()

    def test_get_description(self):
        config = {"rules": [{"name": "test_coupling", "type": "coupling"}]}
        loader = MenuRuleLoader()
        rules = loader.load_from_dict(config)
        desc = rules[0].get_description()
        assert 'coupling' in desc
        assert 'test_coupling' in desc

    def test_rule_repr(self):
        config = {"rules": [{"name": "test_repr", "type": "premium",
                              "max_per_day": 1, "min_per_horizon": 1, "max_per_horizon": 2}]}
        loader = MenuRuleLoader()
        rules = loader.load_from_dict(config)
        r = repr(rules[0])
        assert 'PremiumMenuRule' in r
        assert 'test_repr' in r


class TestLoadForClient:
    """Tests for MenuRuleLoader.load_for_client()."""

    def test_missing_file_returns_generic(self, monkeypatch):
        monkeypatch.setattr(
            'api.config.CLIENT_RULES_CONFIG_PATH', '/nonexistent/nope.json')
        loader = MenuRuleLoader()
        generic = [object()]  # dummy rule
        result = loader.load_for_client('Tekion', generic)
        assert result == generic

    def test_unknown_client_returns_generic(self):
        loader = MenuRuleLoader()
        generic = [object()]
        result = loader.load_for_client('UnknownClientXYZ', generic)
        assert result == generic

    def test_tekion_seed_loads_3_rules(self):
        from src.menu_rules.ingredient_ban_rule import IngredientBanRule
        from src.menu_rules.item_frequency_rule import ItemFrequencyRule
        from src.menu_rules.slot_day_restriction_rule import SlotDayRestrictionRule
        loader = MenuRuleLoader()
        result = loader.load_for_client('Tekion', [])
        assert len(result) == 3
        assert isinstance(result[0], IngredientBanRule)
        assert isinstance(result[1], ItemFrequencyRule)
        assert isinstance(result[2], SlotDayRestrictionRule)

    def test_invalid_rule_is_skipped(self, tmp_path):
        import json
        bad_file = tmp_path / 'client_rules.json'
        bad_file.write_text(json.dumps({
            "TestClient": [
                {"name": "bad", "type": "nonexistent_type"},
                {"name": "good", "type": "ingredient_ban", "ingredients": ["egg"]},
            ]
        }))
        from unittest.mock import patch
        with patch('api.config.CLIENT_RULES_CONFIG_PATH', str(bad_file)):
            loader = MenuRuleLoader()
            result = loader.load_for_client('TestClient', [])
        assert len(result) == 1
        assert result[0].name == 'good'


class TestInvalidConfigLogging:
    """The loader should log *why* a rule was dropped so admins can fix it."""

    def test_min_gt_max_item_frequency_logs_reason(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        config = {
            "rules": [{
                "name": "bad_freq", "type": "item_frequency",
                "selector": {"flag": "is_liquid_rice"},
                "min_per_week": 3, "max_per_week": 1,
            }]
        }
        loader = MenuRuleLoader()
        rules = loader.load_from_dict(config)
        assert rules == []
        joined = "\n".join(rec.message for rec in caplog.records)
        assert "bad_freq" in joined
        assert "min_per_week" in joined and "<=" in joined

    def test_invalid_premium_rule_logs_reason(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        config = {
            "rules": [{
                "name": "bad_premium", "type": "premium",
                "min_per_horizon": 5, "max_per_horizon": 1,
            }]
        }
        loader = MenuRuleLoader()
        rules = loader.load_from_dict(config)
        assert rules == []
        joined = "\n".join(rec.message for rec in caplog.records)
        assert "bad_premium" in joined
        assert "min_per_horizon" in joined
