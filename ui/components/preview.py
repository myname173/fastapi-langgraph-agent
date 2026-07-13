"""Preview panel — render agent-generated artifacts in the UI.

Generic artifact renderer: dispatches by ``type`` field (html / image /
video / json / text / other). No task-specific logic — works for any web
project the agent produces (games, dashboards, landing pages, tools...).

Security:
- HTML artifacts render in a sandboxed iframe with ``sandbox="allow-scripts"``
  only (NO ``allow-same-origin``). Per W3C spec, ``allow-scripts`` alone lets
  the page execute JS and load relative-path resources (<script src>,
  <link>, <img>) but blocks fetch/XHR/localStorage — sufficient for static
  web apps/games while preventing same-origin sandbox escape.
- JSON/text content is fetched via httpx (not requests) for dependency
  consistency with the rest of the UI layer.
"""

import httpx
import streamlit as st
import streamlit.components.v1 as components

from ui.config import config

_IFRAME_HEIGHT = 600


def render_preview_tab() -> None:
    """Render the Preview tab.

    Fetches artifacts from the backend via ``get_artifacts`` and renders
    each by type. Shows a friendly placeholder when no artifacts exist
    (e.g. refactor tasks, non-web tasks) — non-intrusive by design.
    """
    client = st.session_state.get("api_client")
    thread_id = st.session_state.get("current_thread_id") or (client.session_id if client else None)

    if not client or not thread_id:
        st.caption("Submit a task to see previews here.")
        return

    artifacts = client.get_artifacts(thread_id)

    if not artifacts:
        st.caption("No previewable artifacts. The agent has not registered any previewable output for this task.")
        return

    st.markdown(f"**{len(artifacts)} artifact(s) registered by the agent.**")

    # Artifact selector when multiple exist.
    if len(artifacts) > 1:
        options = [a.get("name", "unnamed") for a in artifacts]
        selected_idx = st.selectbox(
            "Select artifact",
            range(len(options)),
            format_func=lambda i: options[i],
            key="preview_artifact_select",
        )
        artifact = artifacts[selected_idx]
    else:
        artifact = artifacts[0]

    _render_artifact(artifact)


def _render_artifact(artifact: dict) -> None:
    """Render a single artifact by type."""
    name = artifact.get("name", "Artifact")
    desc = artifact.get("description", "")
    art_type = artifact.get("type", "text")
    preview_url = artifact.get("preview_url", "")

    # Build full URL from UI's api_base_url (browser-accessible) + relative
    # preview_url. Do NOT use backend's absolute_url — it may use localhost.
    full_url = f"{config.api_base_url}{preview_url}" if preview_url else ""

    st.markdown(f"### {name}")
    if desc:
        st.caption(desc)

    if not full_url:
        st.warning("No preview URL available for this artifact.")
        return

    # Dispatch by type.
    if art_type == "html":
        _render_html_preview(full_url)
    elif art_type == "image":
        st.image(full_url)
    elif art_type == "video":
        st.video(full_url)
    elif art_type == "json":
        _render_json_preview(full_url)
    else:
        _render_text_preview(full_url, art_type)

    # Always show the URL for manual access / debugging.
    st.caption(f"Preview URL: `{full_url}`")


def _render_html_preview(url: str) -> None:
    """Render HTML artifact in a sandboxed iframe (allow-scripts only).

    Uses ``components.html`` with a custom <iframe> tag so we can set
    ``sandbox="allow-scripts"`` (without allow-same-origin). Streamlit's
    built-in ``components.iframe`` does not expose a sandbox parameter.
    """
    components.html(
        f'<iframe src="{url}" sandbox="allow-scripts" '
        f'style="width:100%;height:{_IFRAME_HEIGHT}px;border:0;"></iframe>',
        height=_IFRAME_HEIGHT,
    )


def _render_json_preview(url: str) -> None:
    """Fetch and display JSON content as a formatted tree."""
    try:
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code == 200:
            st.json(resp.json())
        else:
            st.warning(f"Failed to load JSON: HTTP {resp.status_code}")
    except Exception as e:
        st.warning(f"Failed to load JSON: {e}")


def _render_text_preview(url: str, art_type: str) -> None:
    """Fetch and display text content as code."""
    try:
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code == 200:
            lang = art_type if art_type not in ("text", "other") else "text"
            st.code(resp.text, language=lang)
        else:
            st.warning(f"Failed to load content: HTTP {resp.status_code}")
    except Exception as e:
        st.warning(f"Failed to load content: {e}")
