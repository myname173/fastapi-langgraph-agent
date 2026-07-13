"""Media generation tools for the SDLC coder agent.

Thin wrappers around ``MediaGenerationService`` — no HTTP logic here,
just LangChain ``@tool`` binding so the coder can generate images and
videos autonomously when a task requires visual assets.

Decoupling: these tools depend only on ``media_service`` (the singleton
from ``app.services.media_generation_service``), never on the HTTP /
aiohttp layer directly. This keeps the tool layer testable and avoids
duplicating DashScope API plumbing.
"""

from langchain_core.tools import tool

from app.services.media_generation_service import media_service


@tool
async def generate_image(prompt: str, size: str = "1024*1024", n: int = 1) -> dict:
    """Generate image(s) from a text prompt using Bailian wan2.6-t2i.

    Use this tool when the task requires creating visual assets such as
    illustrations, UI mockups, diagrams, or icons. Returns image URLs
    that can be embedded in HTML or saved to the workspace.

    Args:
        prompt: Detailed description of the image to generate.
        size: Image size, e.g. "1024*1024" (default) or "1280*720".
        n: Number of images to generate (1-4, default 1).

    Returns:
        Dict with ``images`` (list of URLs) and ``model``.
    """
    return await media_service.generate_image(prompt, size, n)


@tool
async def generate_video(prompt: str, duration: int = 5, resolution: str = "720P") -> dict:
    """Generate a video from a text prompt using Bailian wan2.7-t2v.

    Use this tool when the task requires creating video content.
    Generation is asynchronous (submit + poll) and typically takes 1-5
    minutes. Returns a video URL that can be embedded or linked.

    Args:
        prompt: Detailed description of the video to generate.
        duration: Video length in seconds (2-15, default 5).
        resolution: "720P" (default) or "1080P".

    Returns:
        Dict with ``video_url``, ``task_id``, and ``model``.
    """
    return await media_service.generate_video(prompt, duration, resolution)


# Exported for the registry builder — mirrors the sdlc_tools_list pattern.
media_tools_list = [generate_image, generate_video]
