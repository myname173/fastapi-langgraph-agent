"""Security tests for path traversal and command injection.

Covers:
- get_workspace_path: path traversal attempts (../, absolute, symlink)
- sanitize_command: DANGEROUS_CHARS filter (` , $(, >) + first-token allow-list
- sanitize_command: inert metacharacters (;, &, |, $, ', ", <) pass through
- sanitize_command: disallowed commands (rm, ls, cat, curl)
- sanitize_command: empty command
- validate_file_path: disallowed extensions on write
- is_allowed_extension: allowed vs blocked extensions
"""

import pytest

from app.core.langgraph.tools.sdlc_tools import (
    ALLOWED_TEST_COMMANDS,
    DANGEROUS_CHARS,
    get_workspace_path,
    is_allowed_extension,
    sanitize_command,
    validate_file_path,
)


class TestPathTraversal:
    """Tests for get_workspace_path() — path traversal protection."""

    def test_normal_relative_path_succeeds(self, temp_workspace, thread_id):
        """A normal relative path resolves within the workspace."""
        path = get_workspace_path(thread_id, "src/main.py")
        assert path.is_absolute()
        assert thread_id in str(path)

    def test_dotdot_traversal_blocked(self, temp_workspace, thread_id):
        """../../etc/passwd is rejected."""
        with pytest.raises(ValueError, match="Path traversal detected"):
            get_workspace_path(thread_id, "../../etc/passwd")

    def test_single_dotdot_traversal_blocked(self, temp_workspace, thread_id):
        """../secret is rejected."""
        with pytest.raises(ValueError, match="Path traversal detected"):
            get_workspace_path(thread_id, "../secret")

    def test_absolute_path_blocked(self, temp_workspace, thread_id):
        """An absolute path outside the workspace is rejected."""
        with pytest.raises(ValueError, match="Path traversal detected"):
            get_workspace_path(thread_id, "/etc/passwd")

    def test_nested_dotdot_traversal_blocked(self, temp_workspace, thread_id):
        """Deeply nested ../ sequences are rejected."""
        with pytest.raises(ValueError, match="Path traversal detected"):
            get_workspace_path(thread_id, "src/../../../etc/passwd")

    def test_dotdot_in_middle_blocked(self, temp_workspace, thread_id):
        """../ appearing in the middle of a path is rejected."""
        with pytest.raises(ValueError, match="Path traversal detected"):
            get_workspace_path(thread_id, "src/.././../../etc/passwd")

    def test_current_dir_path_succeeds(self, temp_workspace, thread_id):
        """'.' resolves to the workspace root."""
        path = get_workspace_path(thread_id, ".")
        assert path.is_absolute()
        assert path.exists()

    def test_subdirectory_path_succeeds(self, temp_workspace, thread_id):
        """A path with subdirectories resolves correctly."""
        path = get_workspace_path(thread_id, "src/utils/helper.py")
        assert path.is_absolute()
        # The parent directories don't need to exist for get_workspace_path
        assert "src" in str(path)
        assert "utils" in str(path)


class TestValidateFilePath:
    """Tests for validate_file_path() — extension + existence checks."""

    def test_write_with_allowed_extension_succeeds(self, temp_workspace, thread_id):
        """Writing a .py file is allowed."""
        path = validate_file_path(thread_id, "script.py", write=True)
        assert path.is_absolute()
        assert path.suffix == ".py"

    def test_write_with_disallowed_extension_raises(self, temp_workspace, thread_id):
        """Writing a .exe file is rejected."""
        with pytest.raises(ValueError, match="File extension not allowed"):
            validate_file_path(thread_id, "malware.exe", write=True)

    def test_write_with_sh_extension_raises(self, temp_workspace, thread_id):
        """Writing a .sh file is rejected (not in allowed extensions)."""
        with pytest.raises(ValueError, match="File extension not allowed"):
            validate_file_path(thread_id, "script.sh", write=True)

    def test_read_nonexistent_file_raises(self, temp_workspace, thread_id):
        """Reading a non-existent file raises."""
        with pytest.raises(ValueError, match="File not found"):
            validate_file_path(thread_id, "nonexistent.py", write=False)

    def test_read_existing_file_succeeds(self, temp_workspace, thread_id):
        """Reading an existing file returns the absolute path."""
        # Create the file first
        file_path = get_workspace_path(thread_id, "existing.py")
        file_path.write_text("# test content")

        path = validate_file_path(thread_id, "existing.py", write=False)
        assert path == file_path


class TestIsAllowedExtension:
    """Tests for is_allowed_extension()."""

    def test_python_extension_allowed(self):
        """.py is in the allowed list."""
        from pathlib import Path

        assert is_allowed_extension(Path("script.py")) is True

    def test_json_extension_allowed(self):
        """.json is in the allowed list."""
        from pathlib import Path

        assert is_allowed_extension(Path("config.json")) is True

    def test_md_extension_allowed(self):
        """.md is in the allowed list."""
        from pathlib import Path

        assert is_allowed_extension(Path("README.md")) is True

    def test_exe_extension_blocked(self):
        """.exe is not in the allowed list."""
        from pathlib import Path

        assert is_allowed_extension(Path("program.exe")) is False

    def test_sh_extension_blocked(self):
        """.sh is not in the allowed list (shell scripts blocked)."""
        from pathlib import Path

        assert is_allowed_extension(Path("script.sh")) is False

    def test_no_extension_blocked(self):
        """Files without extension are blocked."""
        from pathlib import Path

        assert is_allowed_extension(Path("Makefile")) is False


class TestSanitizeCommand:
    """Tests for sanitize_command() — command injection protection.

    Contract (S-2 fix): the returned argv is executed via
    ``asyncio.create_subprocess_exec`` — never through a shell — so shell
    metacharacters (``;``, ``|``, ``&``, quotes, a bare ``$``, ``<``) arrive as
    inert literal arguments and are therefore *not* rejected. Only constructs
    that stay dangerous on a shell-free backend are filtered by
    ``DANGEROUS_CHARS`` (backtick, ``$(``, ``>``); the real gate is the
    ``ALLOWED_TEST_COMMANDS`` allow-list applied to the first token.
    """

    # --- accepted commands -------------------------------------------------

    def test_valid_pytest_command(self):
        """A plain 'pytest -v' command is allowed."""
        args = sanitize_command("pytest -v")
        assert args == ["pytest", "-v"]

    def test_valid_python_command(self):
        """A 'python script.py' command is allowed."""
        args = sanitize_command("python script.py")
        assert args == ["python", "script.py"]

    def test_valid_python3_command(self):
        """A 'python3 -m pytest' command is allowed."""
        args = sanitize_command("python3 -m pytest")
        assert args == ["python3", "-m", "pytest"]

    def test_legit_sequenced_command_allowed(self):
        """A legitimate ``cmd ; echo ...`` sequence is allowed (S-2 rationale)."""
        args = sanitize_command("python fib.py 5 ; echo done")
        assert args == ["python", "fib.py", "5", ";", "echo", "done"]

    # --- genuinely blocked: DANGEROUS_CHARS --------------------------------

    def test_command_substitution_blocked(self):
        """``$(...)`` command substitution is rejected."""
        with pytest.raises(ValueError, match="dangerous characters"):
            sanitize_command("pytest $(whoami)")

    def test_backtick_injection_blocked(self):
        """Backtick (command substitution) is rejected."""
        with pytest.raises(ValueError, match="dangerous characters"):
            sanitize_command("pytest `whoami`")

    def test_output_redirect_blocked(self):
        """``>`` output redirection is rejected."""
        with pytest.raises(ValueError, match="dangerous characters"):
            sanitize_command("pytest > /tmp/output")

    # --- inert metacharacters: allowed, because exec never uses a shell -----

    def test_semicolon_is_inert(self):
        """``;`` is passed through as a literal argv element (no shell)."""
        assert sanitize_command("python script.py ; echo done") == [
            "python",
            "script.py",
            ";",
            "echo",
            "done",
        ]

    def test_semicolon_glued_to_command_name_rejected(self):
        """``pytest; rm -rf /`` is stopped by the allow-list, not the char filter.

        The first token is ``pytest;`` (not ``pytest``), so it never matches
        ALLOWED_TEST_COMMANDS and the second command cannot run.
        """
        with pytest.raises(ValueError, match="Command not allowed"):
            sanitize_command("pytest; rm -rf /")

    def test_pipe_is_inert(self):
        """``|`` is passed through as a literal argv element (no shell)."""
        assert sanitize_command("pytest | cat") == ["pytest", "|", "cat"]

    def test_ampersand_is_inert(self):
        """``&`` is passed through as a literal argv element (no shell)."""
        assert sanitize_command("pytest & background_job") == ["pytest", "&", "background_job"]

    def test_bare_dollar_is_inert(self):
        """A bare ``$`` (parameter expansion) is inert without a shell."""
        assert sanitize_command("pytest $HOME") == ["pytest", "$HOME"]

    def test_quotes_are_inert(self):
        """Single/double quotes are inert without a shell."""
        assert sanitize_command("pytest 'arg'") == ["pytest", "'arg'"]
        assert sanitize_command('pytest "arg"') == ["pytest", '"arg"']

    def test_input_redirect_is_inert(self):
        """``<`` is passed through as a literal argv element (no shell)."""
        assert sanitize_command("pytest < /etc/passwd") == ["pytest", "<", "/etc/passwd"]

    def test_disallowed_command_blocked(self):
        """Rm is not in ALLOWED_TEST_COMMANDS."""
        with pytest.raises(ValueError, match="Command not allowed"):
            sanitize_command("rm -rf /")

    def test_ls_command_blocked(self):
        """Ls is not in ALLOWED_TEST_COMMANDS."""
        with pytest.raises(ValueError, match="Command not allowed"):
            sanitize_command("ls -la")

    def test_curl_command_blocked(self):
        """Curl is not in ALLOWED_TEST_COMMANDS (network exfiltration)."""
        with pytest.raises(ValueError, match="Command not allowed"):
            sanitize_command("curl evil.com")

    def test_empty_command_blocked(self):
        """An empty command is rejected."""
        with pytest.raises(ValueError, match="Empty command"):
            sanitize_command("")

    def test_whitespace_only_command_blocked(self):
        """A whitespace-only command is rejected."""
        with pytest.raises(ValueError, match="Empty command"):
            sanitize_command("   ")

    def test_allowed_commands_constant(self):
        """ALLOWED_TEST_COMMANDS contains exactly pytest, python, python3."""
        assert ALLOWED_TEST_COMMANDS == ["pytest", "python", "python3"]

    def test_dangerous_chars_pattern_targets_only_real_threats(self):
        """DANGEROUS_CHARS matches `` ` ``, ``$(`` and ``>`` — and nothing else.

        Guards against someone re-widening the pattern to the inert
        metacharacters, which would re-break legitimate tester commands such as
        ``python fib.py 5 ; echo done``.
        """
        import re

        for seq in ["`", "$(", ">"]:
            assert re.search(DANGEROUS_CHARS, f"pytest {seq} cmd"), f"DANGEROUS_CHARS should match {seq!r}"
        for seq in [";", "&", "|", "$", "'", '"', "<"]:
            assert not re.search(DANGEROUS_CHARS, f"pytest {seq} cmd"), f"DANGEROUS_CHARS should NOT match {seq!r}"


class TestCommandRunnerShellSafety:
    """Regression guard for the invariant behind the narrowed DANGEROUS_CHARS contract.

    Test commands are executed with ``create_subprocess_exec`` (shell-free). If
    anyone switches to a shell, ``;`` / ``|`` / ``&`` instantly become injection
    vectors again, so the sanitizer would have to be re-tightened.
    """

    def test_sdlc_tools_never_spawns_a_shell(self):
        """sdlc_tools.py must not use shell=True / create_subprocess_shell."""
        import importlib
        import inspect

        # NB: `from app.core.langgraph.tools import sdlc_tools` yields the
        # backward-compat *list* re-exported by the package __init__, not the
        # module — import_module bypasses that shadowing.
        mod = importlib.import_module("app.core.langgraph.tools.sdlc_tools")
        src = inspect.getsource(mod)
        assert "shell=True" not in src, "sanitize_command's contract assumes a shell-free exec"
        assert "create_subprocess_shell" not in src

    def test_sandbox_runner_never_spawns_a_shell(self):
        """The Docker sandbox runner must also stay shell-free."""
        import importlib
        import inspect

        mod = importlib.import_module("app.core.sandbox.runner")
        src = inspect.getsource(mod)
        assert "shell=True" not in src
        assert "create_subprocess_shell" not in src
