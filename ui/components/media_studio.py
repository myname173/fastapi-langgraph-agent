"""Media Studio component.

Provides a Streamlit UI for text-to-image generation and a history view of
previously generated media. Video generation is intentionally **not** exposed
in the UI — generated videos were not reliably mapped to the originating
feature/role, so the video tab has been removed to avoid showing unrelated
media.

Images are tagged with the feature/role they were generated for (inferred
from the prompt) and can be re-run ("续跑") once quota is available, so a user
can resume a task from the generated picture.
"""

import streamlit as st

from ui.api_client import SDLCAPIClient

# Keyword → feature/role mapping used to label generated images. Order matters:
# the first matching rule wins. Extend as new agent roles / features are added.
ROLE_RULES = [
    ("coder", ["代码", "code", "bug", "函数", "function", "脚本", "script", "后端", "backend", "api"]),
    ("reviewer", ["审查", "review", "评审", "测试", "test", "质量", "quality"]),
    ("researcher", ["研究", "research", "调研", "分析", "analysis", "搜索", "search"]),
    ("frontend", ["前端", "frontend", "ui", "界面", "页面", "网页", "component"]),
    ("architecture", ["架构", "architecture", "设计图", "拓扑", "topology", "系统图", "diagram"]),
    ("design", ["设计", "design", "logo", "海报", "poster", "配色", "视觉", "visual"]),
    ("product", ["产品", "product", "需求", "prd", "原型", "prototype"]),
]


def _infer_role(prompt: str) -> str:
    """Infer the feature/role a prompt belongs to, from keyword rules.

    Falls back to "general" when no rule matches.
    """
    if not prompt:
        return "general"
    text = prompt.lower()
    for role, keywords in ROLE_RULES:
        for kw in keywords:
            if kw.lower() in text:
                return role
    return "general"


ROLE_LABELS = {
    "coder": "💻 编码 (coder)",
    "reviewer": "🔍 审查 (reviewer)",
    "researcher": "📚 研究 (researcher)",
    "frontend": "🎨 前端 (frontend)",
    "architecture": "🏗️ 架构 (architecture)",
    "design": "✨ 设计 (design)",
    "product": "📦 产品 (product)",
    "general": "🧩 通用 (general)",
}


def _role_badge(prompt: str) -> str:
    """Return the human-readable role label for a prompt."""
    return ROLE_LABELS.get(_infer_role(prompt), ROLE_LABELS["general"])


def _queue_rerun(prompt: str) -> None:
    """Store a prompt so the Image tab picks it up and regenerates it (续跑)."""
    st.session_state["media_rerun_prompt"] = prompt
    st.session_state["media_tab"] = "Image"
    st.rerun()


def tab_image(client: SDLCAPIClient):
    """Text-to-image generation tab.

    Pre-fills the prompt box when a 续跑 (re-run) request is queued from
    history, so the user can regenerate the same image once quota allows.
    """
    st.subheader("🖼️ Text to Image")

    queued = st.session_state.get("media_rerun_prompt")
    default_prompt = queued if queued else ""

    prompt = st.text_area(
        "Prompt",
        value=default_prompt,
        height=100,
        help="描述你想要的图片。生成后会自动标注它对应的功能/角色。",
        key="media_image_prompt",
    )

    col1, col2 = st.columns([1, 3])
    with col1:
        n_images = st.number_input("数量", min_value=1, max_value=4, value=1, key="media_image_n")
    with col2:
        st.caption("生成完成后，图片会显示其负责的功能/角色，可一键续跑。")

    if st.button("生成图片", key="media_image_btn", use_container_width=True):
        # Consume any queued re-run prompt so it doesn't sticky-resubmit.
        st.session_state.pop("media_rerun_prompt", None)
        if not prompt.strip():
            st.warning("请输入 prompt")
            return
        with st.spinner("生成中…"):
            result = client.generate_image(prompt=prompt, n_images=n_images)
        if not result:
            st.error("生成失败，请稍后重试")
            return
        items = result.get("results", [])
        if not items:
            st.error(result.get("error", "未返回图片"))
            return
        st.success(f"生成完成（{len(items)} 张）")
        for item in items:
            _render_image_item(item, prompt)

    # If a re-run was queued but the user hasn't submitted yet, surface a hint.
    if queued and not st.session_state.get("_media_rerun_hinted"):
        st.info("已载入续跑 prompt，点击「生成图片」即可重新生成（需额度充足）。")
        st.session_state["_media_rerun_hinted"] = True


def _render_image_item(item: dict, fallback_prompt: str = "") -> None:
    """Render a single generated image with its role tag and 续跑 button."""
    image_url = item.get("image_url") or item.get("url")
    prompt = item.get("prompt") or fallback_prompt
    role = _role_badge(prompt)
    cols = st.columns([3, 1])
    with cols[0]:
        if image_url:
            try:
                st.image(image_url, use_container_width=True)
            except Exception:
                st.markdown(f"[查看图片]({image_url})")
        else:
            st.warning("图片地址缺失")
    with cols[1]:
        st.badge(role, color="blue")
        if prompt:
            if st.button("续跑", key=f"rerun_{item.get('id', image_url)}", use_container_width=True):
                _queue_rerun(prompt)


def tab_history(client: SDLCAPIClient):
    """History of generated media.

    Only images are rendered — video entries are skipped because video
    generation is no longer exposed in the UI.
    """
    st.subheader("📜 生成历史")

    if st.button("刷新", key="media_hist_refresh"):
        st.session_state.pop("media_history_cache", None)

    cache = st.session_state.get("media_history_cache")
    if cache is None:
        with st.spinner("加载历史…"):
            cache = client.get_media_history(limit=50) or []
        st.session_state.media_history_cache = cache

    if not cache:
        st.info("暂无生成记录")
        return

    image_items = [m for m in cache if m.get("media_type") == "image"]
    video_items = [m for m in cache if m.get("media_type") == "video"]

    if not image_items:
        st.info("暂无图片生成记录")
    else:
        for item in image_items:
            prompt = item.get("prompt", "")
            st.divider()
            st.badge(_role_badge(prompt), color="blue")
            st.caption(f"Prompt: {prompt}")
            _render_image_item(item, prompt)

    if video_items:
        st.divider()
        st.caption(f"⚠️ 历史中有 {len(video_items)} 条视频记录，当前界面已不再展示视频，如需查看请前往后端媒体接口。")


def render_media_studio():
    """Render the Media Studio with image generation and history tabs."""
    st.header("🎬 Media Studio")

    client: SDLCAPIClient = st.session_state.api_client

    # Reset the one-shot re-run hint once we land here.
    st.session_state.pop("_media_rerun_hinted", None)

    if not client.is_authenticated:
        st.warning("请先登录以使用 Media Studio")
        return

    tabs = st.tabs(["Image", "History"])
    with tabs[0]:
        tab_image(client)
    with tabs[1]:
        tab_history(client)
