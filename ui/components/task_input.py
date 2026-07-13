"""Task input form with image upload and preview."""

import base64
from typing import List

import streamlit as st

from ui.config import config


def _images_to_base64(uploaded_files) -> List[str]:
    """Convert uploaded image files to base64 strings."""
    images = []
    for img_file in uploaded_files:
        img_bytes = img_file.read()
        img_b64 = base64.b64encode(img_bytes).decode("utf-8")
        images.append(img_b64)
        img_file.seek(0)
    return images


def render_task_input():
    """Render the task input form.

    Collapsible: auto-collapses when a task is running to save space
    for the execution log.
    """
    task_running = st.session_state.get("task_started", False)
    expanded = not task_running

    with st.expander("Submit New Task", expanded=expanded):
        with st.form("task_form"):
            requirements = st.text_area(
                "Requirements",
                height=120,
                placeholder="Describe what you want the SDLC agents to build...",
                help="Be specific about the feature, bug fix, or refactoring you need.",
            )

            project_context = st.text_area(
                "Project Context (optional)",
                height=60,
                placeholder="Provide context about your project structure, tech stack, etc.",
            )

            uploaded_images = st.file_uploader(
                "Upload Images (optional)",
                type=["png", "jpg", "jpeg", "webp"],
                accept_multiple_files=True,
                help=f"Upload UI screenshots, architecture diagrams, or bug screenshots. Max {config.max_images} images.",
            )

            if uploaded_images:
                st.markdown("**Preview:**")
                cols = st.columns(min(len(uploaded_images), config.max_images))
                for i, img_file in enumerate(uploaded_images[: config.max_images]):
                    with cols[i]:
                        st.image(img_file, caption=img_file.name, use_container_width=True)

                if len(uploaded_images) > config.max_images:
                    st.warning(f"Maximum {config.max_images} images allowed. Extra images will be ignored.")

            submitted = st.form_submit_button("Start Task", type="primary", use_container_width=True)

        if submitted and requirements.strip():
            st.session_state.code_changes = []
            st.session_state.plan = []
            st.session_state.tool_calls = []
            st.session_state.human_approval_needed = False
            st.session_state.is_complete = False
            st.session_state.stream_error = None
            st.session_state.final_messages = []
            st.session_state.file_reviews = {}
            st.session_state.test_results = []
            st.session_state.feedback = []
            st.session_state.log_entries = []
            st.session_state.node_timings = {}

            images_b64 = _images_to_base64(uploaded_images) if uploaded_images else None
            if images_b64 and len(images_b64) > config.max_images:
                images_b64 = images_b64[: config.max_images]

            st.session_state.current_requirements = requirements
            st.session_state.current_context = project_context
            st.session_state.current_images = images_b64
            st.session_state.task_started = True
            st.session_state.current_thread_id = None
            st.rerun()
