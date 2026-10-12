"""Tests for pebble.core.safety — is_command_blocked and sanitize_command."""

from pebble.core.safety import is_command_blocked, sanitize_command


class TestIsCommandBlocked:
    def test_rm_rf_root_blocked(self):
        result = is_command_blocked("rm -rf /")
        assert result is not None
        assert "Blocked" in result

    def test_rm_rf_star_blocked(self):
        result = is_command_blocked("rm -rf /*")
        assert result is not None

    def test_mkfs_blocked(self):
        result = is_command_blocked("mkfs /dev/sda")
        assert result is not None

    def test_shutdown_blocked(self):
        result = is_command_blocked("shutdown -h now")
        assert result is not None

    def test_fork_bomb_blocked(self):
        result = is_command_blocked(":(){ :|:& };:")
        assert result is not None

    def test_dd_if_blocked(self):
        result = is_command_blocked("dd if=/dev/zero of=/dev/sda")
        assert result is not None

    def test_safe_ls_returns_none(self):
        assert is_command_blocked("ls -la") is None

    def test_safe_git_returns_none(self):
        assert is_command_blocked("git status") is None

    def test_safe_python_returns_none(self):
        assert is_command_blocked("python script.py") is None

    def test_safe_rm_specific_file(self):
        assert is_command_blocked("rm file.txt") is None

    def test_whitespace_preserved(self):
        # Leading/trailing whitespace is stripped before checking
        result = is_command_blocked("  rm -rf /  ")
        assert result is not None


class TestSanitizeCommand:
    def test_left_single_curly_quote_replaced(self):
        assert sanitize_command("\u2018hello\u2019") == "'hello'"

    def test_double_curly_quotes_replaced(self):
        assert sanitize_command("\u201chello\u201d") == '"hello"'

    def test_en_dash_replaced(self):
        assert sanitize_command("ls \u2013la") == "ls -la"

    def test_em_dash_replaced(self):
        assert sanitize_command("cmd \u2014flag") == "cmd -flag"

    def test_plain_ascii_unchanged(self):
        cmd = "git commit -m 'test'"
        assert sanitize_command(cmd) == cmd

    def test_mixed_replacements(self):
        cmd = "\u201ctest\u201d \u2013flag"
        assert sanitize_command(cmd) == '"test" -flag'


class TestDataHeredocsAndWordBoundaries:
    """A session writing a test file whose content mentioned "shutdown" was
    blocked twice, then rebuilt the word at runtime to slip past the guard.
    File content written via a quoted cat/tee heredoc is data, not a command."""

    def test_quoted_cat_heredoc_body_is_data(self):
        cmd = "cat > t.py <<'EOF'\nassert 'server shutdown' in log\nEOF\npython3 t.py"
        assert is_command_blocked(cmd) is None

    def test_tee_heredoc_body_is_data(self):
        assert is_command_blocked('tee notes.md <<"END"\nhow to reboot safely\nEND') is None

    def test_heredoc_into_an_interpreter_is_still_scanned(self):
        assert is_command_blocked("bash <<'EOF'\nshutdown -h now\nEOF") is not None
        assert (
            is_command_blocked("python3 <<'EOF'\nimport os; os.system('reboot')\nEOF") is not None
        )

    def test_unquoted_heredoc_is_still_scanned(self):
        # Unquoted heredocs expand $(...), so the body can execute.
        assert is_command_blocked("cat > f <<EOF\n$(shutdown -h now)\nEOF") is not None

    def test_commands_after_the_heredoc_are_still_scanned(self):
        assert is_command_blocked("cat > f <<'EOF'\nhello\nEOF\nreboot") is not None

    def test_word_patterns_respect_boundaries(self):
        assert is_command_blocked("echo asphalt") is None
        assert is_command_blocked("grep graceful_shutdown app.py") is None
        assert is_command_blocked("sudo shutdown -h now") is not None
        assert is_command_blocked("systemctl halt") is not None

    def test_block_message_says_report_not_rephrase(self):
        msg = is_command_blocked("reboot")
        assert msg is not None and "tell the user" in msg and "do not rephrase" in msg
