"""Tests for SDLC tools: generate_and_run_script, save_output_file, security."""

import pytest

from app.core.langgraph.tools.sdlc_tools import (
    generate_and_run_script,
    save_output_file,
    get_workspace_path,
    sanitize_command,
    check_security,
)


class TestGenerateAndRunScript:
    """Tests for generate_and_run_script tool."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_workspace, enable_script_execution, thread_id):
        """Set up test fixtures."""
        self.thread_id = thread_id
        self.workspace = temp_workspace

    async def test_successful_script_execution(self):
        """Test that a valid Python script executes successfully."""
        script = "print('Hello from test script')\nresult = 1 + 1\nprint(f'Result: {result}')"
        result = await generate_and_run_script.ainvoke(
            {
                "thread_id": self.thread_id,
                "script_content": script,
                "script_name": "hello.py",
            }
        )

        assert result["success"] is True
        assert result["return_code"] == 0
        assert "Hello from test script" in result["stdout"]
        assert "Result: 2" in result["stdout"]
        assert result["timeout"] is False
        assert result["error"] is None

    async def test_script_with_error(self):
        """Test that a script with an error returns failure."""
        script = "print('before error')\nraise ValueError('test error')"
        result = await generate_and_run_script.ainvoke(
            {
                "thread_id": self.thread_id,
                "script_content": script,
                "script_name": "error_script.py",
            }
        )

        assert result["success"] is False
        assert result["return_code"] != 0
        assert "test error" in result["stderr"]
        assert result["timeout"] is False

    async def test_script_saved_to_scripts_dir(self):
        """Test that the script is saved to the scripts/ directory."""
        script = "print('test')"
        await generate_and_run_script.ainvoke(
            {
                "thread_id": self.thread_id,
                "script_content": script,
                "script_name": "saved.py",
            }
        )

        script_path = self.workspace / self.thread_id / "scripts" / "saved.py"
        assert script_path.exists()
        assert "print('test')" in script_path.read_text()

    async def test_disabled_feature_flag(self, disable_script_execution):
        """Test that disabled feature flag prevents execution."""
        result = await generate_and_run_script.ainvoke(
            {
                "thread_id": self.thread_id,
                "script_content": "print('hello')",
            }
        )

        assert result["success"] is False
        assert "disabled" in result["error"].lower()

    async def test_unsupported_language(self):
        """Test that non-Python languages are rejected."""
        result = await generate_and_run_script.ainvoke(
            {
                "thread_id": self.thread_id,
                "script_content": "console.log('hello')",
                "language": "javascript",
            }
        )

        assert result["success"] is False
        assert "Unsupported language" in result["error"]

    async def test_script_name_auto_extension(self):
        """Test that .py extension is auto-added if missing."""
        script = "print('test')"
        await generate_and_run_script.ainvoke(
            {
                "thread_id": self.thread_id,
                "script_content": script,
                "script_name": "no_extension",
            }
        )

        script_path = self.workspace / self.thread_id / "scripts" / "no_extension.py"
        assert script_path.exists()

    async def test_outputs_dir_created(self):
        """Test that script execution workspace is set up correctly."""
        script = "print('test')"
        result = await generate_and_run_script.ainvoke(
            {
                "thread_id": self.thread_id,
                "script_content": script,
            }
        )
        # The scripts/ directory is created on the host; outputs/ is inside
        # the Docker sandbox container (working_dir="/outputs").
        scripts_dir = self.workspace / self.thread_id / "scripts"
        assert scripts_dir.exists(), f"Scripts dir not created. Result: {result}"
        assert result["success"] is True


class TestSaveOutputFile:
    """Tests for save_output_file tool."""

    @pytest.fixture(autouse=True)
    def setup(self, temp_workspace, thread_id):
        """Set up test fixtures."""
        self.thread_id = thread_id
        self.workspace = temp_workspace

    def test_save_file_success(self):
        """Test saving an output file."""
        result = save_output_file.invoke(
            {
                "thread_id": self.thread_id,
                "content": "test output content",
                "filename": "result.txt",
            }
        )

        assert result["success"] is True
        assert result["file_path"] == "outputs/result.txt"
        assert result["error"] is None

        file_path = self.workspace / self.thread_id / "outputs" / "result.txt"
        assert file_path.exists()
        assert file_path.read_text() == "test output content"

    def test_save_file_with_subdirectory(self):
        """Test saving to a subdirectory within outputs."""
        result = save_output_file.invoke(
            {
                "thread_id": self.thread_id,
                "content": '{"key": "value"}',
                "filename": "data.json",
                "subdirectory": "data",
            }
        )

        assert result["success"] is True
        assert result["file_path"] == "outputs/data/data.json"

        file_path = self.workspace / self.thread_id / "outputs" / "data" / "data.json"
        assert file_path.exists()

    def test_save_file_creates_parent_dirs(self):
        """Test that parent directories are auto-created."""
        result = save_output_file.invoke(
            {
                "thread_id": self.thread_id,
                "content": "nested content",
                "filename": "deep.txt",
                "subdirectory": "a/b/c",
            }
        )

        assert result["success"] is True
        file_path = self.workspace / self.thread_id / "outputs" / "a" / "b" / "c" / "deep.txt"
        assert file_path.exists()

    def test_save_file_rejects_disallowed_extension(self):
        """save_output_file must reject extensions outside the whitelist.

        Mirrors write_file / download_asset. Without this check an agent
        could drop an arbitrary ``.exe`` / ``.sh`` into the workspace.
        """
        result = save_output_file.invoke(
            {
                "thread_id": self.thread_id,
                "content": "malicious",
                "filename": "payload.exe",
            }
        )

        assert result["success"] is False
        assert "not allowed" in result["error"].lower()
        # Nothing should have been written to disk.
        file_path = self.workspace / self.thread_id / "outputs" / "payload.exe"
        assert not file_path.exists()

    def test_save_file_rejects_oversized_content(self, monkeypatch):
        """save_output_file must reject content over the size cap."""
        from app.core.config import settings

        big = "x" * 100  # 100 bytes
        monkeypatch.setattr(settings, "SDLC_MAX_FILE_SIZE", 10)
        result = save_output_file.invoke(
            {
                "thread_id": self.thread_id,
                "content": big,
                "filename": "big.txt",
            }
        )

        assert result["success"] is False
        assert "exceeds" in result["error"].lower()


class TestSecurityValidation:
    """Tests for security validation functions."""

    def test_sanitize_command_allows_python(self):
        """Test that python command is allowed."""
        result = sanitize_command("python script.py")
        assert result == ["python", "script.py"]

    def test_sanitize_command_allows_pytest(self):
        """Test that pytest command is allowed."""
        result = sanitize_command("pytest -v")
        assert result == ["pytest", "-v"]

    def test_sanitize_command_rejects_dangerous_chars(self):
        """Test that dangerous characters are rejected."""
        with pytest.raises(ValueError, match="dangerous characters"):
            sanitize_command("python script.py; rm -rf /")

    def test_sanitize_command_rejects_pipe(self):
        """Test that pipe character is rejected."""
        with pytest.raises(ValueError, match="dangerous characters"):
            sanitize_command("python script.py | cat")

    def test_sanitize_command_rejects_unknown_command(self):
        """Test that unknown commands are rejected."""
        with pytest.raises(ValueError, match="not allowed"):
            sanitize_command("bash script.sh")

    def test_sanitize_command_rejects_empty(self):
        """Test that empty commands are rejected."""
        with pytest.raises(ValueError, match="Empty command"):
            sanitize_command("")

    def test_check_security_passes_clean_code(self):
        """Test that clean code passes security check."""
        check_security("print('hello world')")

    def test_get_workspace_path_rejects_traversal(self, temp_workspace, thread_id):
        """Test that path traversal is detected."""
        with pytest.raises(ValueError, match="Path traversal"):
            get_workspace_path(thread_id, "../../../etc/passwd")

    def test_get_workspace_path_resolves_correctly(self, temp_workspace, thread_id):
        """Test that valid paths resolve within workspace."""
        path = get_workspace_path(thread_id, "scripts/test.py")
        assert "workspace" in str(path)
        assert thread_id in str(path)
