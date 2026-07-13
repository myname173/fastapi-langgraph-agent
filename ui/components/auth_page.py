"""Login and registration page."""

import streamlit as st

from ui.api_client import (
    APIError,
    ConnectionError,
    SDLCAPIClient,
)
from ui.config import config
from ui.state import (
    check_server_health_cached,
    persist_auth,
)


def render_auth_page():
    """Render the login/registration page."""
    st.title("SDLC Orchestrator")
    st.markdown("---")

    client: SDLCAPIClient = st.session_state.api_client

    if not check_server_health_cached(client):
        st.error(f"Cannot connect to API at {config.api_base_url}. Is the server running? (`make dev`)")
        if st.button("Retry Connection", use_container_width=True):
            check_server_health_cached(client, force=True)
            st.rerun()
        st.markdown("---")

    tab_login, tab_register = st.tabs(["Login", "Register"])

    with tab_login:
        with st.form("login_form"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Login", use_container_width=True)

            if submitted:
                try:
                    if client.login(email, password):
                        persist_auth(client)
                        st.session_state.server_online = True
                        st.success("Login successful!")
                        st.rerun()
                except ConnectionError as e:
                    st.session_state.server_online = False
                    st.error(str(e))
                except APIError as e:
                    st.error(str(e))

    with tab_register:
        with st.form("register_form"):
            email = st.text_input("Email", key="reg_email")
            username = st.text_input("Username", key="reg_username")
            password = st.text_input("Password", type="password", key="reg_password")
            submitted = st.form_submit_button("Register", use_container_width=True)

            if submitted:
                try:
                    if client.register(email, username, password):
                        persist_auth(client)
                        st.success("Registration successful!")
                        st.rerun()
                except ConnectionError as e:
                    st.session_state.server_online = False
                    st.error(str(e))
                except APIError as e:
                    st.error(str(e))

    st.markdown("---")
    st.markdown(f"**API URL:** `{config.api_base_url}`")
    st.markdown("Make sure the FastAPI server is running (`make dev`)")
