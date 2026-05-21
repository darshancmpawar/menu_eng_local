"""
Multi-Category Editor -- Configure double (or more) items per category.

Example: Rippling has veg_dry x2, Stripe has nonveg_main x2.
"""

import streamlit as st
from typing import Dict, List

from ui.formatters import prettify_slot_name


def render_multi_slot_editor(
    active_base_slots: List[str],
    current_slot_counts: Dict[str, int],
    const_slots: List[str],
    client_name: str = "",
) -> Dict[str, int]:
    """Render category count editor. Returns updated slot_counts dict."""

    st.markdown(
        '<div class="section-card">'
        '<p class="section-title">Item Frequency</p>'
        '<p class="section-desc">'
        'Set count to 2+ for categories that need duplicates '
        '(e.g. Veg Dry 1 &amp; Veg Dry 2).</p>',
        unsafe_allow_html=True,
    )

    editable = [s for s in active_base_slots if s not in const_slots]

    if not editable:
        st.info("No active categories to configure.")
        st.markdown('</div>', unsafe_allow_html=True)
        return current_slot_counts

    updated = dict(current_slot_counts)

    cols = st.columns(3)
    for idx, slot in enumerate(editable):
        with cols[idx % 3]:
            current = current_slot_counts.get(slot, 1)
            val = st.number_input(
                prettify_slot_name(slot),
                min_value=1,
                max_value=3,
                value=current,
                step=1,
                key=f"editor_slotcount_{client_name}_{slot}",
            )
            updated[slot] = val

    multi_slots = [s for s in editable if updated.get(s, 1) > 1]
    if multi_slots:
        tags = ', '.join(
            f"**{prettify_slot_name(s)}** x{updated[s]}" for s in multi_slots
        )
        st.markdown(
            f'<p style="font-size:0.75rem;color:#86efac;margin:0.5rem 0 0;">'
            f'Multi-categories: {tags}</p>',
            unsafe_allow_html=True,
        )

    st.markdown('</div>', unsafe_allow_html=True)

    return updated
