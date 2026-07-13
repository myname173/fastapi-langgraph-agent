"""Media Studio — image/video generation and history."""

import time

import streamlit as st

from ui.api_client import SDLCAPIClient


def render_media_studio():
    """Render the Media Studio panel for image/video generation.

    Decoupled from SDLC — calls ``client.generate_image`` /
    ``client.generate_video`` directly. No shared state with the task
    workflow.
    """
    st.subheader("Media Studio")
    st.caption("Generate images and videos via Bailian wan2.6-t2i / wan2.7-t2v")

    client: SDLCAPIClient = st.session_state.api_client
    st.session_state.setdefault("media_img_history", [])
    st.session_state.setdefault("media_vid_history", [])

    tab_image, tab_video, tab_history = st.tabs(["Text to Image", "Text to Video", "History"])

    img_examples = [
        "\u4e00\u53ea\u6a58\u732b\u5750\u5728\u7a97\u53f0\u4e0a\uff0c\u9633\u5149\u6d12\u5728\u6bdb\u53d1\u4e0a\uff0c\u6e29\u99a8\u67d4\u548c",
        " minimalist UI dashboard, dark theme, blue accents, clean layout",
        "A samurai standing in a bamboo forest at sunset, cinematic lighting",
    ]
    vid_examples = [
        "\u732b\u54aa\u5728\u9633\u5149\u4e0b\u6253\u76f9\uff0c\u7f13\u6162\u547c\u5438\uff0c\u6e29\u99a8\u5bb6\u5c45\u573a\u666f",
        "\u57ce\u5e02\u591c\u666f\u5ef6\u65f6\u6444\u5f71\uff0c\u8f66\u6d41\u7a7f\u68ad\uff0c\u9713\u8679\u706f\u95ea\u70c1",
        "Ocean waves crashing on rocks at golden hour, slow motion",
    ]

    with tab_image:
        with st.expander("Example prompts (click to use)", expanded=False):
            ex_cols = st.columns(len(img_examples))
            for col, ex in zip(ex_cols, img_examples, strict=False):
                if col.button(ex[:30] + "\u2026", key=f"img_ex_{hash(ex)}", help=ex):
                    st.session_state.media_img_prompt = ex

        img_prompt = st.text_area(
            "Prompt",
            key="media_img_prompt",
            height=80,
            placeholder="Describe the image you want to generate...",
        )
        col1, col2 = st.columns(2)
        with col1:
            size_options = {
                "Square 1024\u00d71024": "1024*1024",
                "Landscape 1280\u00d7720": "1280*720",
                "Portrait 720\u00d71280": "720*1280",
            }
            size_label = st.selectbox("Size", list(size_options.keys()), key="media_img_size")
            img_size = size_options[size_label]
        with col2:
            img_n = st.number_input("Count", min_value=1, max_value=4, value=1, key="media_img_n")

        if st.button("Generate Image", key="media_img_btn", type="primary", disabled=not img_prompt.strip()):
            with st.spinner("Generating image..."):
                result = client.generate_image(img_prompt, img_size, int(img_n))
            if result and result.get("images"):
                for url in result["images"]:
                    st.image(url)
                    st.download_button(
                        label="Download image",
                        data=client.download_bytes(url),
                        file_name=f"generated_{int(time.time())}.png",
                        mime="image/png",
                        key=f"dl_img_{hash(url)}",
                    )
                st.session_state.media_img_history = (result["images"] + st.session_state.media_img_history)[:10]
            else:
                st.error("Image generation failed. Check Bailian account quota.")

        if st.session_state.media_img_history:
            st.markdown("**History**")
            hist_cols = st.columns(min(len(st.session_state.media_img_history), 4))
            for col, url in zip(hist_cols, st.session_state.media_img_history, strict=False):
                col.image(url)

    with tab_video:
        with st.expander("Example prompts (click to use)", expanded=False):
            ex_cols = st.columns(len(vid_examples))
            for col, ex in zip(ex_cols, vid_examples, strict=False):
                if col.button(ex[:30] + "\u2026", key=f"vid_ex_{hash(ex)}", help=ex):
                    st.session_state.media_vid_prompt = ex

        vid_prompt = st.text_area(
            "Prompt",
            key="media_vid_prompt",
            height=80,
            placeholder="Describe the video you want to generate...",
        )
        col1, col2 = st.columns(2)
        with col1:
            vid_duration = st.number_input(
                "Duration (seconds)", min_value=2, max_value=15, value=5, key="media_vid_dur"
            )
        with col2:
            vid_res = st.selectbox("Resolution", ["720P", "1080P"], key="media_vid_res")

        if st.button("Generate Video", key="media_vid_btn", type="primary", disabled=not vid_prompt.strip()):
            with st.spinner("Generating video (may take 1-5 minutes)..."):
                result = client.generate_video(vid_prompt, int(vid_duration), vid_res)
            if result and result.get("video_url"):
                st.video(result["video_url"])
                st.caption(f"Task ID: {result.get('task_id', '')}")
                st.session_state.media_vid_history = ([result] + st.session_state.media_vid_history)[:5]
            else:
                st.error("Video generation failed. Check Bailian account quota.")

        if st.session_state.media_vid_history:
            st.markdown("**History**")
            for item in st.session_state.media_vid_history:
                st.video(item["video_url"])
                st.caption(f"Task ID: {item.get('task_id', '')}")

    with tab_history:
        st.markdown("**Generation History** (server-side, L-14)")
        st.caption(
            "Persisted across sessions. DashScope image/video URLs expire "
            "in 24-48h \u2014 expired links will appear but may not load."
        )
        col_refresh, col_limit = st.columns([1, 2])
        with col_refresh:
            if st.button("Refresh", key="media_hist_refresh"):
                st.session_state.media_hist_loaded = False
        with col_limit:
            hist_limit = st.number_input(
                "Max results",
                min_value=10,
                max_value=200,
                value=50,
                step=10,
                key="media_hist_limit",
            )

        if not st.session_state.get("media_hist_loaded"):
            with st.spinner("Loading history..."):
                st.session_state.media_hist_records = client.fetch_media_history(int(hist_limit))
            st.session_state.media_hist_loaded = True

        records = st.session_state.get("media_hist_records", [])
        if not records:
            st.info("No generation history yet. Generate an image or video to see it here.")
        else:
            for r in records:
                status_icon = "\u2705" if r.get("status") == "success" else "\u274c"
                type_label = r.get("media_type", "?").upper()
                ts = r.get("created_at", "")
                ts_short = ts[:16].replace("T", " ") if ts else ""
                with st.container(border=True):
                    header = f"{status_icon} {type_label} \u2014 {ts_short}"
                    if r.get("model"):
                        header += f" \u00b7 {r['model']}"
                    st.markdown(f"**{header}**")
                    st.write(r.get("prompt", "")[:200])
                    if r.get("status") == "success":
                        url = r.get("result_url")
                        if url:
                            if type_label == "IMAGE":
                                st.image(url)
                            else:
                                st.video(url)
                            if r.get("task_id"):
                                st.caption(f"Task ID: {r['task_id']}")
                        if r.get("image_count", 0) > 1:
                            st.caption(f"({r['image_count']} images generated)")
                    else:
                        if r.get("error_message"):
                            st.error(r["error_message"][:300])
                        if r.get("task_id"):
                            st.caption(f"Task ID: {r['task_id']} (may still be runnable)")
