"""
Theme Editor -- Customize day-wise menu theme per client.

Global defaults: Mon=Mix, Tue=Chinese, Wed=Biryani, Thu=South, Fri=North.
Each client can override any day to any of the 5 themes.
"""

import streamlit as st
from typing import Dict, List

from ui.formatters import THEME_TAG_COLORS, THEME_ICONS

_WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']

_THEME_DISPLAY = {
    'mix': 'Mix (South + North)',
    'chinese': 'Chinese',
    'biryani': 'Biryani',
    'south': 'South Indian',
    'north': 'North Indian',
}


def render_theme_editor(
    current_theme_map: Dict[str, str],
    default_theme_map: Dict[str, str],
    available_themes: List[str],
    client_name: str = "",
) -> Dict[str, str]:
    """Render theme day editor. Returns updated theme_map dict."""

    st.markdown(
        '<div class="section-card">'
        '<p class="section-title">Day Themes</p>'
        '<p class="section-desc">'
        'Override the default cuisine theme for any weekday. '
        'Other clients keep the global defaults.</p>',
        unsafe_allow_html=True,
    )

    updated = {}

    for day in _WEEKDAYS:
        day_display = day.capitalize()
        current_val = current_theme_map.get(day, default_theme_map.get(day, 'mix'))
        default_val = default_theme_map.get(day, 'mix')

        col_day, col_select, col_tag = st.columns([1.2, 2, 1.5])
        with col_day:
            st.markdown(
                f'<p style="font-weight:700;color:#fafafa;margin:0.5rem 0;'
                f'font-size:0.85rem;">{day_display}</p>',
                unsafe_allow_html=True,
            )
        with col_select:
            try:
                default_idx = available_themes.index(current_val)
            except ValueError:
                default_idx = 0
            chosen = st.selectbox(
                f"Theme for {day_display}",
                available_themes,
                index=default_idx,
                format_func=lambda t: _THEME_DISPLAY.get(t, t.title()),
                key=f"editor_theme_{client_name}_{day}",
                label_visibility="collapsed",
            )
            updated[day] = chosen

        with col_tag:
            bg, fg = THEME_TAG_COLORS.get(chosen, ('#27272a', '#71717a'))
            icon = THEME_ICONS.get(chosen, '')
            is_override = (chosen != default_val)
            border = f' border:1px solid {fg};' if is_override else ''
            label = _THEME_DISPLAY.get(chosen, chosen.title())
            st.markdown(
                f'<span style="display:inline-flex;align-items:center;gap:4px;'
                f'margin-top:0.45rem;padding:3px 10px;border-radius:99px;'
                f'font-size:0.65rem;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:0.04em;background:{bg};color:{fg};{border}">'
                f'{icon} {label}{"  *" if is_override else ""}'
                f'</span>',
                unsafe_allow_html=True,
            )

    overrides = {d: t for d, t in updated.items()
                 if t != default_theme_map.get(d)}
    if overrides:
        parts = [f"{d.capitalize()}: {_THEME_DISPLAY.get(t, t)}"
                 for d, t in overrides.items()]
        st.markdown(
            f'<p style="font-size:0.72rem;color:#fbbf24;margin:0.5rem 0 0;">'
            f'Overrides: {" | ".join(parts)}</p>',
            unsafe_allow_html=True,
        )

    st.markdown('</div>', unsafe_allow_html=True)

    return updated
