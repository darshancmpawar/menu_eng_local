"""Tests for UI formatters."""

from ui.formatters import (
    theme_label,
    display_label_for_slot_id,
    flatten_api_solution,
    format_item_for_ui,
    format_item_html,
    pretty_text,
    color_suffix,
    slot_sort_key,
)


def test_theme_label_monday():
    assert theme_label(0) == "Mix of South + North"


def test_theme_label_tuesday():
    assert theme_label(1) == "Chinese / Indo-Chinese"


def test_theme_label_wednesday():
    assert theme_label(2) == "Biryani Day"


def test_theme_label_thursday():
    assert theme_label(3) == "South Indian"


def test_theme_label_friday():
    assert theme_label(4) == "North Indian"


def test_display_label_known_slot():
    label = display_label_for_slot_id("welcome_drink")
    assert isinstance(label, str)
    assert len(label) > 0


def test_display_label_unknown_slot():
    label = display_label_for_slot_id("some_unknown_slot")
    assert "Some Unknown Slot" == label


def test_format_item_for_ui():
    assert format_item_for_ui("  jeera_rice(Y)  ") == "Jeera Rice"
    assert format_item_for_ui("dal_tadka(R)") == "Dal Tadka"
    assert format_item_for_ui("steamed rice") == "Steamed Rice"
    assert format_item_for_ui("") == ""
    assert format_item_for_ui(None) == ""


def test_pretty_text_strips_color():
    assert pretty_text("jeera rice (Y)") == "Jeera Rice"


def test_pretty_text_no_suffix():
    assert pretty_text("paneer butter masala") == "Paneer Butter Masala"


def test_color_suffix_present():
    assert color_suffix("dal makhani (R)") == "R"


def test_color_suffix_absent():
    assert color_suffix("dal makhani") is None


def test_slot_sort_key_known():
    k1 = slot_sort_key("welcome_drink")
    k2 = slot_sort_key("dessert")
    assert k1 < k2


def test_slot_sort_key_with_suffix():
    k = slot_sort_key("veg_dry__1")
    assert k < 999


def test_slot_sort_key_unknown():
    assert slot_sort_key("xyz_slot") == 999


def test_format_item_html_escapes_html_in_item_name():
    # Admins can edit Supabase/Excel, and the rendered output goes into
    # st.markdown(..., unsafe_allow_html=True). The item name must be
    # HTML-escaped so tag-like strings render as text, not markup.
    out = format_item_html("<script>alert(1)</script>(Y)")
    lower = out.lower()
    assert "<script>" not in lower
    assert "</script>" not in lower
    assert "&lt;script&gt;" in lower
    assert "&lt;/script&gt;" in lower
    # Structural markup we emit ourselves still passes through.
    assert '<span class="item-name">' in out
    assert '<span class="color-pill"' in out


def test_format_item_html_escapes_without_color_suffix():
    out = format_item_html("<b>bold</b>")
    lower = out.lower()
    assert "<b>" not in lower
    assert "</b>" not in lower
    assert "&lt;b&gt;" in lower
    assert "&lt;/b&gt;" in lower


def test_flatten_api_solution_rich_format():
    raw = {
        "2026-03-23": {
            "theme": "mix",
            "day_type": "mix",
            "items": {
                "bread": {"item": "plain_chapatti(B)", "item_base": "plain_chapatti"},
                "rice": {"item": "jeera_rice(Y)"},
            },
        },
    }
    flat, day_types = flatten_api_solution(raw)
    assert flat == {"2026-03-23": {"bread": "plain_chapatti(B)", "rice": "jeera_rice(Y)"}}
    assert day_types == {"2026-03-23": "mix"}


def test_flatten_api_solution_flat_legacy_format():
    raw = {"2026-03-23": {"bread": "plain_chapatti(B)"}}
    flat, day_types = flatten_api_solution(raw)
    assert flat == {"2026-03-23": {"bread": "plain_chapatti(B)"}}
    assert day_types == {}


def test_flatten_api_solution_empty():
    assert flatten_api_solution({}) == ({}, {})


def test_flatten_api_solution_falls_back_to_item_base():
    raw = {
        "2026-03-23": {
            "day_type": "south",
            "items": {"bread": {"item_base": "plain_chapatti"}},
        },
    }
    flat, _ = flatten_api_solution(raw)
    assert flat["2026-03-23"]["bread"] == "plain_chapatti"
