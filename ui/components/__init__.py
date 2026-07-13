"""UI component modules for the SDLC Orchestrator web interface.

Each module exposes a single primary ``render_*`` function:
- ``auth_page.render_auth_page`` — login/register
- ``session_manager.render_session_manager`` — enhanced sidebar
- ``task_input.render_task_input`` — task submission form
- ``streaming.render_streaming_output`` — real-time SSE output with
  structured logging and stage timeline
- ``log_viewer.render_log_area`` — structured log viewer with view
  modes (Verbose/Normal/Summary); also provides ``append_log_entry``,
  ``render_stage_timeline``, and timing helpers consumed by streaming
- ``diff_viewer.render_diff_viewer`` — visual code change review with
  per-file accept/reject markers
- ``results.render_results`` — results panel + approval workflow
- ``media_studio.render_media_studio`` — image/video generation
"""
