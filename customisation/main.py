"""
Customisation Editor -- Main page that orchestrates all editor sections.

Two flows:
  Select Existing: select client -> edit categories/frequency/themes -> Save | Reset
  Create New:      enter name -> pick categories/frequency/themes -> Create Client | Reset Setup

Called from app.py when st.session_state.view == "editor".
"""

import streamlit as st
from ui.api_client import MenuApiClient
from customisation.slot_editor import render_slot_editor
from customisation.multi_slot_editor import render_multi_slot_editor
from customisation.theme_editor import render_theme_editor

def _inject_editor_css():
    st.markdown("""
    <style>
        .editor-header {
            display: flex; align-items: center; gap: 1rem;
            margin-bottom: 1.75rem;
        }
        .editor-title {
            font-size: 1.55rem; font-weight: 800; color: #fafafa;
            letter-spacing: -0.5px; margin: 0; line-height: 1.2;
        }
        .editor-subtitle {
            font-size: 0.8rem; color: #71717a; margin: 0.15rem 0 0;
            font-weight: 400;
        }
        .section-card {
            background: #111113; border: 1px solid #27272a;
            border-radius: 14px; padding: 1.25rem 1.5rem;
            margin-bottom: 1rem;
        }
        .section-title {
            font-size: 1rem; font-weight: 700; color: #fafafa;
            margin: 0 0 0.15rem; letter-spacing: -0.2px;
        }
        .section-desc {
            font-size: 0.75rem; color: #71717a; margin: 0 0 1rem;
        }
        .status-pill {
            display: inline-block; padding: 2px 8px;
            border-radius: 99px; font-size: 0.68rem; font-weight: 600;
        }
        .status-pill.match { background: #0f2a1d; color: #86efac; }
        .status-pill.new   { background: #2a1508; color: #fdba74; }
        .status-pill.warn  { background: #2a1508; color: #fdba74; }
        .changes-indicator {
            display: inline-flex; align-items: center; gap: 0.35rem;
            padding: 0.3rem 0.75rem; background: rgba(251,191,36,0.08);
            border: 1px solid rgba(251,191,36,0.15); border-radius: 99px;
            font-size: 0.75rem; color: #fbbf24; font-weight: 500;
            margin-bottom: 0.75rem;
        }
    </style>
    """, unsafe_allow_html=True)


def render_customisation_editor(api: MenuApiClient):
    """Main entry point for the customisation editor view."""
    _inject_editor_css()

    # --- Top bar ---
    col_back, col_title = st.columns([1, 5])
    with col_back:
        if st.button("< Back to Menu", key="editor_back_btn", use_container_width=True):
            st.session_state.view = "planner"
            st.rerun()
    with col_title:
        st.markdown(
            '<div><p class="editor-title">Customisation Editor</p>'
            '<p class="editor-subtitle">Create or edit clients, categories, frequency, and day themes</p>'
            '</div>',
            unsafe_allow_html=True,
        )

    st.markdown("")

    # --- Show success message ---
    if st.session_state.get('editor_success_msg'):
        st.success(st.session_state.pop('editor_success_msg'))

    # --- Load metadata ---
    try:
        metadata = api.get_editor_metadata()
    except Exception as e:
        st.error(f"Failed to load editor data: {e}")
        return

    clients = metadata.get('clients', [])
    all_base_slots = metadata.get('base_slot_names', [])
    const_slots = metadata.get('const_slots', [])
    default_theme_map = metadata.get('default_theme_map', {})
    available_themes = metadata.get('available_themes', [])
    menu_categories = metadata.get('menu_categories', {})

    # ============================================================
    # Section 1: Client Management
    # ============================================================
    st.markdown(
        '<div class="section-card">'
        '<p class="section-title">Client</p>'
        '<p class="section-desc">Select an existing client or create a new one</p>',
        unsafe_allow_html=True,
    )

    mode = st.radio(
        "Mode",
        ["Select Existing", "Create New"],
        horizontal=True,
        key="editor_mode",
        label_visibility="collapsed",
    )
    is_create_mode = (mode == "Create New")

    selected_client = None
    new_client_name = ""

    if not is_create_mode:
        if not clients:
            st.info("No clients found. Switch to **Create New** to add one.")
            st.markdown('</div>', unsafe_allow_html=True)
            return
        selected_client = st.selectbox(
            "Client", clients,
            key="editor_client_select",
            label_visibility="collapsed",
        )
    else:
        new_client_name = st.text_input(
            "Client Name", key="editor_new_client_name",
            placeholder="e.g. Acme Corp",
        )

    st.markdown('</div>', unsafe_allow_html=True)

    # For Select Existing: load config from DB
    # For Create New: use defaults
    if not is_create_mode:
        try:
            config = api.get_client_config(selected_client)
        except Exception as e:
            st.error(f"Failed to load config for {selected_client}: {e}")
            return
        current_active = config.get('active_base_slots', [])
        current_counts = config.get('slot_counts', {})
        current_theme = config.get('theme_map', dict(default_theme_map))
        # Optimistic-concurrency counter returned by GET /client-config.
        # Every PUT that modifies this client must send it back so two
        # admins editing at once can't last-write-wins silently.
        current_version = config.get('version')
        client_key = selected_client
    else:
        if not new_client_name.strip():
            st.markdown(
                '<div style="text-align:center;padding:2rem;color:#52525b;">'
                'Enter a client name above to start configuring.</div>',
                unsafe_allow_html=True,
            )
            return
        current_active = [s for s in all_base_slots if s not in set(const_slots)]
        current_counts = {s: 1 for s in all_base_slots}
        current_theme = dict(default_theme_map)
        current_version = None  # no row yet; set after api.create_client
        client_key = "_new_"

    # ============================================================
    # Section 2: Customize Categories
    # ============================================================
    new_active_slots = render_slot_editor(
        all_base_slots, current_active, const_slots, client_key,
    )

    # Show auto-mapped menu category
    if new_active_slots:
        sorted_selected = sorted(new_active_slots)
        matched_cat = None
        for cat_name, cat_slots in menu_categories.items():
            if sorted(cat_slots) == sorted_selected:
                matched_cat = cat_name
                break
        if matched_cat:
            st.markdown(
                f'<span class="status-pill match">Mapped to {matched_cat}</span>',
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                '<span class="status-pill new">New category will be created</span>',
                unsafe_allow_html=True,
            )

    # ============================================================
    # Section 3: Item Frequency
    # ============================================================
    new_slot_counts = render_multi_slot_editor(
        new_active_slots, current_counts, const_slots, client_key,
    )

    # ============================================================
    # Section 4: Day-wise Theme Override
    # ============================================================
    new_theme_map = render_theme_editor(
        current_theme, default_theme_map, available_themes, client_key,
    )

    # ============================================================
    # Action bar
    # ============================================================
    st.markdown("")

    # Unsaved changes indicator
    if not is_create_mode:
        changes = []
        if set(new_active_slots) != set(current_active):
            changes.append("categories")
        count_changes = {k: v for k, v in new_slot_counts.items()
                         if v != current_counts.get(k, 1) and k in new_active_slots}
        if count_changes:
            changes.append("frequency")
        theme_changes = {k: v for k, v in new_theme_map.items()
                         if v != current_theme.get(k)}
        if theme_changes:
            changes.append("themes")
        if changes:
            st.markdown(
                f'<div class="changes-indicator">&#9679; Unsaved: {", ".join(changes)}</div>',
                unsafe_allow_html=True,
            )

    if is_create_mode:
        col_create, col_reset = st.columns(2)

        with col_create:
            create_clicked = st.button(
                "Create Client", type="primary",
                key="editor_create_btn", use_container_width=True,
            )

        with col_reset:
            reset_clicked = st.button(
                "Reset Setup",
                key="editor_reset_new", use_container_width=True,
            )

        if create_clicked:
            name = new_client_name.strip()
            if not name:
                st.error("Enter a client name.")
            elif name in clients:
                st.error(f"Client '{name}' already exists.")
            elif not new_active_slots:
                st.error("Select at least one category.")
            else:
                try:
                    api.create_client(name, new_active_slots)
                    freq_overrides = {k: v for k, v in new_slot_counts.items()
                                      if k in new_active_slots and v != 1}
                    theme_overrides = {k: v for k, v in new_theme_map.items()
                                       if v != default_theme_map.get(k)}
                    if freq_overrides or theme_overrides:
                        # Fetch the just-created config to pick up its
                        # starting version (server-side default is 1).
                        fresh = api.get_client_config(name)
                        payload = {'version': fresh.get('version', 1)}
                        if freq_overrides:
                            payload['slot_counts'] = new_slot_counts
                        if theme_overrides:
                            payload['theme_map'] = new_theme_map
                        api.update_client_config(name, payload)
                    # Invalidate the planner sidebar's cached client list
                    # so the new client shows up immediately rather than
                    # 60s later when the TTL expires.
                    st.cache_data.clear()
                    st.session_state['editor_success_msg'] = f"Client '{name}' created successfully!"
                    st.session_state.pop('editor_new_client_name', None)
                    st.rerun()
                except Exception as e:
                    st.error(f"Create failed: {e}")

        if reset_clicked:
            for key in list(st.session_state.keys()):
                if '_new_' in key or key == 'editor_new_client_name':
                    st.session_state.pop(key, None)
            st.rerun()

    else:
        col_save, col_reset, col_delete = st.columns(3)

        with col_save:
            save_clicked = st.button(
                "Save", type="primary",
                key="editor_save_all", use_container_width=True,
            )

        with col_reset:
            reset_clicked = st.button(
                "Reset to Defaults",
                key="editor_reset_all", use_container_width=True,
            )

        with col_delete:
            delete_clicked = st.button(
                "Delete Client",
                key="editor_delete_btn", use_container_width=True,
            )

        if delete_clicked:
            try:
                api.delete_client(selected_client)
                # Invalidate cached client list so the deleted client
                # disappears from the picker immediately.
                st.cache_data.clear()
                st.session_state['editor_success_msg'] = f"Client '{selected_client}' deleted."
                st.rerun()
            except Exception as e:
                st.error(f"Delete failed: {e}")

        if save_clicked:
            payload = {'version': current_version}
            if set(new_active_slots) != set(current_active):
                payload['active_base_slots'] = new_active_slots
            count_overrides = {k: v for k, v in new_slot_counts.items()
                               if k in new_active_slots}
            payload['slot_counts'] = count_overrides
            payload['theme_map'] = new_theme_map
            try:
                api.update_client_config(selected_client, payload)
                st.session_state['editor_success_msg'] = f"Configuration saved for {selected_client}"
                st.rerun()
            except Exception as e:
                # 409 surfaces here as an HTTPError with "modified by
                # another request" in the message — tell the user to
                # refresh rather than hide the conflict.
                st.error(f"Save failed: {e}")

        if reset_clicked:
            payload = {
                'version': current_version,
                'active_base_slots': list(all_base_slots),
                'slot_counts': {s: 1 for s in all_base_slots},
                'theme_map': dict(default_theme_map),
            }
            try:
                api.update_client_config(selected_client, payload)
                st.session_state['editor_success_msg'] = f"Reset {selected_client} to defaults"
                st.rerun()
            except Exception as e:
                st.error(f"Reset failed: {e}")
