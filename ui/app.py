"""SDLC Orchestrator Web UI — Streamlit Application entry point.

Thin orchestrator: delegates rendering to focused component modules.
Layout: sidebar (session manager) + main area (tabs: SDLC / Media).

SDLC tab uses a two-column workspace layout:
  - Left column: task input + streaming execution log
  - Right column: results panel (plan, code, tests, review, approval)

Usage:
    streamlit run ui/app.py
"""

import streamlit as st

from ui.components.auth_page import render_auth_page
from ui.components.media_studio import render_media_studio
from ui.components.results import render_results
from ui.components.session_manager import render_session_manager
from ui.components.streaming import render_streaming_output
from ui.components.task_input import render_task_input
from ui.state import init_session_state


def main():
    """Main Streamlit application entry point."""
    st.set_page_config(
        page_title="SDLC Orchestrator",
        page_icon=":rocket:",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_session_state()

    client = st.session_state.api_client

    # Auth gate — unauthenticated users see the login page only.
    if not client.is_authenticated:
        render_auth_page()
        return

    # Sidebar: session manager (account, tasks, flags, logout).
    render_session_manager()

    # Main area: top-level tabs.
    tab_sdlc, tab_media = st.tabs(["SDLC Tasks", "Media Studio"])

    with tab_sdlc:
        # Two-column workspace: input+execution | results
        col_workspace, col_results = st.columns([3, 2], gap="large")
        with col_workspace:
            render_task_input()
            render_streaming_output()
        with col_results:
            render_results()

    with tab_media:
        render_media_studio()


if __name__ == "__main__":
    main()
