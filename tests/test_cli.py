"""Unit tests for openreview_dl.cli module."""

import http.client
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import pytest

from openreview_dl import cli
from openreview_dl.cli import (
    build_passkey_url,
    decrypt,
    encrypt,
    extract_reviewer_id,
    get_config_dir,
    get_credentials_path,
    get_key,
    get_passkey_port,
    get_unique_filename,
    install_passkey_flow,
    is_headless,
    parse_openreview_url,
    passkey_browser_flow,
)

class TestGetKey:
    """Tests for get_key function - validates cross-platform compatibility."""

    def test_returns_bytes(self):
        """Key should be bytes."""
        key = get_key()
        assert isinstance(key, bytes)

    def test_key_is_consistent(self):
        """Same machine should produce same key."""
        key1 = get_key()
        key2 = get_key()
        assert key1 == key2

    def test_key_is_valid_fernet_key(self):
        """Key should be valid for Fernet encryption."""
        from cryptography.fernet import Fernet

        key = get_key()
        # Should not raise an exception
        Fernet(key)


class TestEncryptDecrypt:
    """Tests for encrypt/decrypt round-trip."""

    def test_roundtrip_simple(self):
        """Encrypting then decrypting should return original text."""
        original = "test message"
        encrypted = encrypt(original)
        decrypted = decrypt(encrypted)
        assert decrypted == original

    def test_roundtrip_unicode(self):
        """Should handle unicode characters."""
        original = "test with émojis 🎉 and ünïcödé"
        encrypted = encrypt(original)
        decrypted = decrypt(encrypted)
        assert decrypted == original

    def test_roundtrip_json(self):
        """Should handle JSON-like strings."""
        import json

        original = json.dumps({"username": "test@example.com", "password": "secret123"})
        encrypted = encrypt(original)
        decrypted = decrypt(encrypted)
        assert decrypted == original

    def test_encrypted_differs_from_original(self):
        """Encrypted text should not equal original."""
        original = "test message"
        encrypted = encrypt(original)
        assert encrypted != original


class TestGetConfigDir:
    """Tests for get_config_dir function."""

    def test_returns_path(self):
        """Should return a Path object."""
        result = get_config_dir()
        assert isinstance(result, Path)

    def test_path_ends_with_openreview_dl(self):
        """Path should end with openreview-dl."""
        result = get_config_dir()
        assert result.name == "openreview-dl"

    def test_parent_is_config(self):
        """Parent directory should be .config."""
        result = get_config_dir()
        assert result.parent.name == ".config"


class TestGetCredentialsPath:
    """Tests for get_credentials_path function."""

    def test_returns_path(self):
        """Should return a Path object."""
        result = get_credentials_path()
        assert isinstance(result, Path)

    def test_filename_is_credentials_enc(self):
        """Filename should be credentials.enc."""
        result = get_credentials_path()
        assert result.name == "credentials.enc"


class TestGetUniqueFilename:
    """Tests for get_unique_filename function."""

    def test_returns_original_if_not_exists(self):
        """Should return original filename if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, "test.txt")
            result = get_unique_filename(filename)
            assert result == filename

    def test_appends_counter_if_exists(self):
        """Should append counter if file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, "test.txt")
            # Create the file
            Path(filename).touch()
            result = get_unique_filename(filename)
            expected = os.path.join(tmpdir, "test_1.txt")
            assert result == expected

    def test_increments_counter(self):
        """Should increment counter for multiple existing files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, "test.txt")
            # Create multiple files
            Path(filename).touch()
            Path(os.path.join(tmpdir, "test_1.txt")).touch()
            Path(os.path.join(tmpdir, "test_2.txt")).touch()
            result = get_unique_filename(filename)
            expected = os.path.join(tmpdir, "test_3.txt")
            assert result == expected


class TestParseOpenreviewUrl:
    """Tests for parse_openreview_url function."""

    def test_extracts_forum_id(self):
        """Should extract forum ID from URL."""
        url = "https://openreview.net/forum?id=abc123&referrer=%5BHomepage%5D(%2Fgroup%3Fid%3DVenue%2F2024)"
        forum_id, venue_id = parse_openreview_url(url)
        assert forum_id == "abc123"

    def test_extracts_venue_id(self):
        """Should extract venue ID from referrer parameter."""
        # Real OpenReview URLs have the referrer URL-encoded
        url = "https://openreview.net/forum?id=abc123&referrer=%5BHomepage%5D(%2Fgroup%3Fid%3DNeurIPS.cc%2F2024%2FConference)"
        forum_id, venue_id = parse_openreview_url(url)
        # The regex captures everything after id= including the trailing )
        assert venue_id == "NeurIPS.cc/2024/Conference)"

    def test_missing_forum_id(self):
        """Should return None for missing forum ID."""
        url = "https://openreview.net/forum?referrer=something"
        forum_id, venue_id = parse_openreview_url(url)
        assert forum_id is None

    def test_missing_referrer(self):
        """Should return None for missing venue ID when no referrer."""
        url = "https://openreview.net/forum?id=abc123"
        forum_id, venue_id = parse_openreview_url(url)
        assert forum_id == "abc123"
        assert venue_id is None


class TestExtractReviewerId:
    """Tests for extract_reviewer_id function."""

    def test_extracts_reviewer_id(self):
        """Should extract reviewer ID from signature."""
        signature = ["Venue/2024/Conference/Submission123/Reviewer_ABC"]
        result = extract_reviewer_id(signature)
        assert result == "ABC"

    def test_extracts_program_committee_id(self):
        """Should extract Program Committee ID from signature."""
        signature = ["Venue/2024/Conference/Submission123/Program_Committee_XYZ"]
        result = extract_reviewer_id(signature)
        assert result == "XYZ"

    def test_returns_none_for_non_reviewer(self):
        """Should return None for non-reviewer signatures."""
        signature = ["Venue/2024/Conference/Authors"]
        result = extract_reviewer_id(signature)
        assert result is None

    def test_handles_numeric_ids(self):
        """Should handle numeric reviewer IDs."""
        signature = ["Venue/2024/Conference/Submission123/Reviewer_1"]
        result = extract_reviewer_id(signature)
        assert result == "1"


class TestIsHeadless:
    """Tests for is_headless - decides whether a browser can be opened."""

    GUI_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER", "SSH_CONNECTION", "SSH_TTY")

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        for var in self.GUI_VARS + (cli.NO_BROWSER_ENV,):
            monkeypatch.delenv(var, raising=False)

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
    def test_env_override_forces_headless(self, monkeypatch, value):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv(cli.NO_BROWSER_ENV, value)
        assert is_headless() is True

    @pytest.mark.parametrize("value", ["0", "false", "", "no"])
    def test_env_override_falsy_is_ignored(self, monkeypatch, value):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv(cli.NO_BROWSER_ENV, value)
        assert is_headless() is False

    def test_linux_without_display_is_headless(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert is_headless() is True

    @pytest.mark.parametrize("var", ["DISPLAY", "WAYLAND_DISPLAY", "BROWSER"])
    def test_linux_with_display_or_browser_is_not_headless(self, monkeypatch, var):
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv(var, "something")
        assert is_headless() is False

    @pytest.mark.parametrize("platform_name", ["linux", "darwin", "win32"])
    @pytest.mark.parametrize("ssh_var", ["SSH_CONNECTION", "SSH_TTY"])
    @pytest.mark.parametrize("gui_var", [None, "DISPLAY", "BROWSER"])
    def test_ssh_session_is_always_headless(self, monkeypatch, platform_name, ssh_var, gui_var):
        """X forwarding or a BROWSER command over SSH cannot reach the user's passkey."""
        monkeypatch.setattr(sys, "platform", platform_name)
        monkeypatch.setenv(ssh_var, "set")
        if gui_var:
            monkeypatch.setenv(gui_var, "localhost:10.0")
        assert is_headless() is True

    @pytest.mark.parametrize("platform_name", ["darwin", "win32"])
    def test_desktop_platforms_default_to_gui(self, monkeypatch, platform_name):
        monkeypatch.setattr(sys, "platform", platform_name)
        assert is_headless() is False


class TestGetPasskeyPort:
    """Tests for get_passkey_port - reads OPENREVIEW_DL_PASSKEY_PORT."""

    def test_unset_means_any_free_port(self, monkeypatch):
        monkeypatch.delenv(cli.PASSKEY_PORT_ENV, raising=False)
        assert get_passkey_port() == 0

    def test_blank_means_any_free_port(self, monkeypatch):
        monkeypatch.setenv(cli.PASSKEY_PORT_ENV, "  ")
        assert get_passkey_port() == 0

    def test_valid_port(self, monkeypatch):
        monkeypatch.setenv(cli.PASSKEY_PORT_ENV, " 8123 ")
        assert get_passkey_port() == 8123

    @pytest.mark.parametrize("value", ["abc", "12.5", "-1", "65536"])
    def test_invalid_port_raises(self, monkeypatch, value):
        monkeypatch.setenv(cli.PASSKEY_PORT_ENV, value)
        with pytest.raises(ValueError, match=cli.PASSKEY_PORT_ENV):
            get_passkey_port()


class TestBuildPasskeyUrl:
    """Tests for build_passkey_url."""

    def test_builds_webauthn_url(self):
        url = build_passkey_url("https://api2.openreview.net", "tok.en", 54321)
        assert url == (
            "https://api2.openreview.net/mfa/webauthn-auth"
            "?pendingToken=tok.en&callbackPort=54321"
        )

    def test_strips_trailing_slash_and_quotes_token(self):
        url = build_passkey_url("https://api2.example.org/", "a+b/c=d&e", 1)
        parsed = urllib.parse.urlparse(url)
        assert parsed.path == "/mfa/webauthn-auth"
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
        assert query == {"pendingToken": ["a+b/c=d&e"], "callbackPort": ["1"]}


PROFILE = {
    "id": "~Test_User1",
    "content": {
        "preferredEmail": "test@example.org",
        "emails": ["test@example.org", "Alt@Example.org"],
        "emailsConfirmed": ["test@example.org"],
        "names": [{"username": "~Test_User1"}, {"username": "~Tess_User2"}],
    },
}


class _FakeResponse:
    def __init__(self, status_code, payload=None, raise_on_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise = raise_on_json

    def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self.error:
            raise self.error
        return self.response


class _FakeClient:
    baseurl = "https://api2.example.org"

    def __init__(self, session=None):
        self.session = session or _FakeSession(_FakeResponse(200, {"profiles": [PROFILE]}))
        self.headers = {"User-Agent": "test", "Accept": "application/json"}


class TestTokenOwner:
    """Tests for token_owner - asks OpenReview who a token belongs to."""

    def test_returns_profile_for_valid_token(self):
        client = _FakeClient()
        assert cli.token_owner(client, "tok") == (PROFILE, None, True)
        call = client.session.calls[0]
        assert call["url"] == "https://api2.example.org/profiles"
        assert call["headers"]["Authorization"] == "Bearer tok"
        assert call["headers"]["User-Agent"] == "test"
        assert call["timeout"]

    def test_401_is_a_definitive_rejection(self):
        client = _FakeClient(_FakeSession(_FakeResponse(401, {"message": "nope"})))
        assert cli.token_owner(client, "tok") == (None, "OpenReview rejected it (HTTP 401)", True)

    @pytest.mark.parametrize("status", [403, 429, 500, 503])
    def test_other_statuses_are_not_definitive(self, status):
        client = _FakeClient(_FakeSession(_FakeResponse(status, {"name": "ChallengeRequiredError"})))
        assert cli.token_owner(client, "tok") == (None, f"OpenReview answered HTTP {status}", False)

    def test_network_error_is_not_blamed_on_the_token(self):
        client = _FakeClient(_FakeSession(error=ConnectionError("down")))
        assert cli.token_owner(client, "tok") == (
            None,
            "could not reach OpenReview to verify it (ConnectionError: down)",
            False,
        )

    def test_non_json_body_is_not_definitive(self):
        client = _FakeClient(_FakeSession(_FakeResponse(200, raise_on_json=True)))
        assert cli.token_owner(client, "tok") == (None, "OpenReview sent an unreadable reply", False)

    @pytest.mark.parametrize(
        "payload",
        [
            {"profiles": []},
            {"profiles": None},
            {},
            {"profiles": [{"content": {}}]},
            {"profiles": [{"id": ""}]},
            {"profiles": ["~Test_User1"]},
        ],
    )
    def test_missing_or_malformed_profile_is_definitive(self, payload):
        client = _FakeClient(_FakeSession(_FakeResponse(200, payload)))
        assert cli.token_owner(client, "tok") == (None, "OpenReview returned no profile for it", True)

    def test_list_body_is_unreadable(self):
        client = _FakeClient(_FakeSession(_FakeResponse(200, [PROFILE])))
        assert cli.token_owner(client, "tok") == (None, "OpenReview sent an unreadable reply", False)


class TestProfileMatchesUsername:
    """Tests for _profile_matches_username."""

    @pytest.mark.parametrize(
        "username",
        [None, "", "~Test_User1", "test@example.org", "ALT@example.org", " ~tess_user2 "],
    )
    def test_matching_identifiers(self, username):
        assert cli._profile_matches_username(PROFILE, username) is True

    @pytest.mark.parametrize("username", ["~Other1", "other@example.org", "test@example.com"])
    def test_non_matching_identifiers(self, username):
        assert cli._profile_matches_username(PROFILE, username) is False

    def test_profile_without_content(self):
        assert cli._profile_matches_username({"id": "~X1"}, "~X1") is True
        assert cli._profile_matches_username({"id": "~X1"}, "x@example.org") is False


class TestCallbackUser:
    """Tests for _callback_user - the user object handed to openreview-py."""

    def test_keeps_well_formed_user(self):
        user = {"id": "~Test_User1", "profile": {"id": "~Test_User1", "extra": 1}}
        assert cli._callback_user(json.dumps(user), PROFILE) == user

    @pytest.mark.parametrize("raw", [None, "", "not json", "null", "[]", '{"id": "~X1"}'])
    def test_rebuilds_unusable_user_from_profile(self, raw):
        assert cli._callback_user(raw, PROFILE) == {"id": "~Test_User1", "profile": PROFILE}

    def test_absurdly_nested_user_falls_back_instead_of_raising(self):
        raw = "[" * 200_000  # json.loads raises RecursionError, not JSONDecodeError
        assert cli._callback_user(raw, PROFILE) == {"id": "~Test_User1", "profile": PROFILE}


class TestPasskeyCallbackDefaults:
    """Pins production defaults that other tests patch away for speed."""

    def test_idle_connections_are_dropped_by_default(self):
        assert cli._PasskeyCallbackHandler.timeout == cli.PASSKEY_CONNECTION_TIMEOUT
        assert 0 < cli.PASSKEY_CONNECTION_TIMEOUT < cli.PASSKEY_TIMEOUT

    def test_port_reuse_is_disabled_on_windows(self):
        assert cli._PasskeyCallbackServer.allow_reuse_address == (sys.platform != "win32")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until(predicate, timeout=5.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def _is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _http(port: int, method: str, fields: dict | None = None):
    """Talk to the callback server directly (no proxies), return (status, body)."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        body = urllib.parse.urlencode(fields).encode() if fields is not None else None
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        conn.request(method, "/", body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode()
    finally:
        conn.close()


def _post_callback(port: int, fields: dict):
    return _http(port, "POST", fields)


def _callback_port_from_output(out: str) -> int:
    url = next(word for word in out.split() if word.startswith("https://"))
    return int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["callbackPort"][0])


class _FlowRunner:
    """Runs passkey_browser_flow in a background thread and collects its result."""

    def __init__(self, timeout=10, client=None, **kwargs):
        self.result = "unset"
        kwargs.setdefault("no_browser", True)
        self.thread = threading.Thread(
            target=self._run, args=(client or _FakeClient(), timeout, kwargs), daemon=True
        )
        self.thread.start()

    def _run(self, client, timeout, kwargs):
        self.result = passkey_browser_flow(client, "pending-token", timeout, **kwargs)

    def join(self, timeout=10):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "passkey flow did not finish"
        return self.result


USER_JSON = json.dumps({"id": "~Test_User1", "profile": {"id": "~Test_User1"}})
EXPECTED_USER = json.loads(USER_JSON)


class TestPasskeyBrowserFlow:
    """Tests for passkey_browser_flow - the local callback round-trip."""

    @pytest.fixture(autouse=True)
    def isolate(self, monkeypatch):
        """Never open a real browser or talk to OpenReview; record browser calls."""
        self.opened = []
        monkeypatch.setattr(cli.webbrowser, "open", lambda url: self.opened.append(url) or True)
        monkeypatch.setattr(cli, "token_owner", lambda client, token: (PROFILE, None, True))
        for var in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER", "SSH_CONNECTION", "SSH_TTY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(cli.PASSKEY_PORT_ENV, raising=False)
        monkeypatch.delenv(cli.NO_BROWSER_ENV, raising=False)

    def test_headless_prints_url_and_forward_instructions(self, capsys):
        port = _free_port()
        runner = _FlowRunner(port=port)
        _wait_until(lambda: _is_listening(port), what="callback server")

        status, body = _post_callback(port, {"token": "tok123", "user": USER_JSON})
        assert status == 200
        assert "Authentication complete" in body

        assert runner.join() == {"token": "tok123", "user": EXPECTED_USER}
        out = capsys.readouterr().out
        assert build_passkey_url(_FakeClient.baseurl, "pending-token", port) in out
        assert f"ssh -L {port}:127.0.0.1:{port}" in out
        assert "--passkey-port" not in out  # the port was fixed by the caller
        assert "openreview-dl --auth" in out
        assert "Passkey login received." in out
        assert self.opened == []

    def test_random_port_is_reported_in_url_and_hint(self, capsys):
        passkey_browser_flow(_FakeClient(), "pending-token", 0.3, no_browser=True, port=0)
        out = capsys.readouterr().out
        port = _callback_port_from_output(out)
        assert port > 0
        assert f"--passkey-port {port}" in out

    def test_times_out_without_callback(self, capsys):
        result = passkey_browser_flow(_FakeClient(), "pending-token", 0.3, no_browser=True, port=0)
        assert result is None
        assert "Timed out" in capsys.readouterr().out

    def test_stray_get_and_bad_post_do_not_end_the_wait(self):
        port = _free_port()
        runner = _FlowRunner(port=port)
        _wait_until(lambda: _is_listening(port), what="callback server")

        status, body = _http(port, "GET")
        assert status == 200
        assert "Waiting for passkey" in body

        status, _ = _post_callback(port, {"user": USER_JSON})
        assert status == 400

        status, _ = _post_callback(port, {"token": "late-token", "user": USER_JSON})
        assert status == 200
        assert runner.join() == {"token": "late-token", "user": EXPECTED_USER}

    def test_malformed_user_json_is_rebuilt_from_profile(self):
        port = _free_port()
        runner = _FlowRunner(port=port)
        _wait_until(lambda: _is_listening(port), what="callback server")
        _post_callback(port, {"token": "tok", "user": "not json"})
        assert runner.join() == {"token": "tok", "user": {"id": "~Test_User1", "profile": PROFILE}}

    def test_unknown_token_is_rejected_and_wait_continues(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli,
            "token_owner",
            lambda client, token: (PROFILE, None, True)
            if token == "good"
            else (None, "OpenReview rejected it (HTTP 401)", True),
        )
        port = _free_port()
        runner = _FlowRunner(port=port)
        _wait_until(lambda: _is_listening(port), what="callback server")

        status, body = _post_callback(port, {"token": "forged", "user": USER_JSON})
        assert status == 403
        assert "Login not accepted" in body
        assert runner.thread.is_alive()

        status, _ = _post_callback(port, {"token": "good", "user": USER_JSON})
        assert status == 200
        assert runner.join() == {"token": "good", "user": EXPECTED_USER}
        out = capsys.readouterr().out
        assert "Ignoring a passkey callback: OpenReview rejected it (HTTP 401)." in out
        assert "still waiting" in out

    def test_token_for_another_account_is_rejected(self, monkeypatch, capsys):
        other = {"id": "~Attacker1", "content": {"emails": ["attacker@example.org"]}}
        monkeypatch.setattr(
            cli,
            "token_owner",
            lambda client, token: (other if token == "theirs" else PROFILE, None, True),
        )
        port = _free_port()
        runner = _FlowRunner(port=port, username="test@example.org")
        _wait_until(lambda: _is_listening(port), what="callback server")

        status, _ = _post_callback(port, {"token": "theirs", "user": USER_JSON})
        assert status == 403
        assert runner.thread.is_alive()

        status, _ = _post_callback(port, {"token": "mine", "user": USER_JSON})
        assert status == 200
        assert runner.join() == {"token": "mine", "user": EXPECTED_USER}
        out = capsys.readouterr().out
        assert "~Attacker1" in out and "test@example.org" in out

    def test_idle_connection_does_not_stall_past_deadline(self, monkeypatch):
        monkeypatch.setattr(cli._PasskeyCallbackHandler, "timeout", 0.3)
        port = _free_port()
        started = time.monotonic()
        runner = _FlowRunner(timeout=1.0, port=port)
        _wait_until(lambda: _is_listening(port), what="callback server")
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            assert runner.join(timeout=5) is None
        assert time.monotonic() - started < 4

    def test_real_post_is_served_behind_idle_connection(self, monkeypatch):
        monkeypatch.setattr(cli._PasskeyCallbackHandler, "timeout", 0.3)
        port = _free_port()
        runner = _FlowRunner(port=port)
        _wait_until(lambda: _is_listening(port), what="callback server")
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            status, _ = _post_callback(port, {"token": "tok", "user": USER_JSON})
            assert status == 200
        assert runner.join() == {"token": "tok", "user": EXPECTED_USER}

    @pytest.mark.parametrize("content_length", ["-1", "abc", str(cli._MAX_CALLBACK_BODY + 1)])
    def test_bad_content_length_is_rejected(self, content_length):
        port = _free_port()
        runner = _FlowRunner(port=port)
        _wait_until(lambda: _is_listening(port), what="callback server")

        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(
                f"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: {content_length}\r\n\r\n".encode()
            )
            assert sock.recv(64).startswith(b"HTTP/1.0 400")

        _post_callback(port, {"token": "tok", "user": USER_JSON})
        assert runner.join() == {"token": "tok", "user": EXPECTED_USER}

    def test_gui_mode_opens_browser_and_prints_url(self, capsys):
        runner = _FlowRunner(no_browser=False, port=0)
        _wait_until(lambda: self.opened, what="webbrowser.open call")
        url = self.opened[0]
        port = int(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["callbackPort"][0])

        _post_callback(port, {"token": "gui-token", "user": USER_JSON})
        assert runner.join() == {"token": "gui-token", "user": EXPECTED_USER}
        out = capsys.readouterr().out
        assert url in out
        assert "ssh -L" not in out

    def test_gui_mode_browser_failure_prints_forward_instructions(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.webbrowser, "open", lambda url: False)
        result = passkey_browser_flow(_FakeClient(), "pending-token", 0.3, no_browser=False, port=0)
        assert result is None
        out = capsys.readouterr().out
        assert "Could not open a browser automatically" in out
        assert "/mfa/webauthn-auth?pendingToken=pending-token" in out
        assert "ssh -L" in out

    def test_port_in_use_returns_none_with_message(self, capsys):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            port = blocker.getsockname()[1]
            result = passkey_browser_flow(_FakeClient(), "pending-token", 1, no_browser=True, port=port)
        assert result is None
        out = capsys.readouterr().out
        assert f"Could not listen on 127.0.0.1:{port}" in out
        assert cli.PASSKEY_PORT_ENV in out

    def test_headless_default_timeout_is_longer(self, monkeypatch, capsys):
        assert cli.HEADLESS_PASSKEY_TIMEOUT > cli.PASSKEY_TIMEOUT
        monkeypatch.setattr(cli, "HEADLESS_PASSKEY_TIMEOUT", 0.3)
        monkeypatch.setattr(cli, "PASSKEY_TIMEOUT", 0.1)

        assert passkey_browser_flow(_FakeClient(), "t", no_browser=True, port=0) is None
        assert "Waiting up to 0.3 seconds" in capsys.readouterr().out

        assert passkey_browser_flow(_FakeClient(), "t", no_browser=False, port=0) is None
        assert "Waiting up to 0.1 seconds" in capsys.readouterr().out

    def test_env_port_is_used_when_port_not_given(self, monkeypatch):
        port = _free_port()
        monkeypatch.setenv(cli.PASSKEY_PORT_ENV, str(port))
        runner = _FlowRunner(port=None)
        _wait_until(lambda: _is_listening(port), what="callback server")
        _post_callback(port, {"token": "env-token", "user": USER_JSON})
        assert runner.join() == {"token": "env-token", "user": EXPECTED_USER}

    def test_auto_detection_headless(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "is_headless", lambda: True)
        passkey_browser_flow(_FakeClient(), "t", 0.3, no_browser=None, port=0)
        assert "ssh -L" in capsys.readouterr().out
        assert self.opened == []

    def test_auto_detection_gui(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "is_headless", lambda: False)
        passkey_browser_flow(_FakeClient(), "t", 0.3, no_browser=None, port=0)
        out = capsys.readouterr().out
        assert "ssh -L" not in out
        assert len(self.opened) == 1 and self.opened[0] in out

    def test_env_no_browser_reaches_flow(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setenv(cli.NO_BROWSER_ENV, "1")
        passkey_browser_flow(_FakeClient(), "t", 0.3, no_browser=None, port=0)
        assert "ssh -L" in capsys.readouterr().out
        assert self.opened == []

    def test_unknown_user_still_gets_url_and_instructions(self, monkeypatch, capsys):
        def no_user():
            raise OSError("No username set in the environment")

        monkeypatch.setattr(cli.getpass, "getuser", no_user)
        monkeypatch.setattr(cli.socket, "gethostname", lambda: "")
        passkey_browser_flow(_FakeClient(), "pending-token", 0.3, no_browser=True, port=0)
        out = capsys.readouterr().out
        port = _callback_port_from_output(out)
        assert f"ssh -L {port}:127.0.0.1:{port} USER@HOST" in out

    @pytest.mark.parametrize("fixed_port", [True, False])
    def test_instructions_fit_in_80_columns(self, capsys, fixed_port):
        url = build_passkey_url(_FakeClient.baseurl, "pending-token", 65535)
        cli._print_headless_passkey_help(url, 65535, fixed_port)  # no socket: 65535 may be busy
        out = capsys.readouterr().out
        assert ("--passkey-port" in out) is not fixed_port  # reuse hint only for a random port
        for line in out.splitlines():
            if "https://" in line or "ssh -L" in line:
                continue
            assert len(line) <= 80, line


class TestInstallPasskeyFlow:
    """Tests for install_passkey_flow - hooking into openreview-py."""

    def test_installs_into_openreview_mfa_module(self, monkeypatch):
        mfa = pytest.importorskip("openreview.mfa")
        original = mfa._passkey_browser_flow
        monkeypatch.setattr(mfa, "_passkey_browser_flow", original)  # restore afterwards

        assert install_passkey_flow(no_browser=True, port=4242, username="me@example.org") is True
        hooked = mfa._passkey_browser_flow
        assert hooked is not original
        assert hooked.func is passkey_browser_flow
        assert hooked.keywords == {"no_browser": True, "port": 4242, "username": "me@example.org"}

    def test_api_client_completes_passkey_through_installed_flow(self, monkeypatch, capsys):
        """End to end: OpenReviewClient's passkey step runs our flow and gets the token."""
        mfa = pytest.importorskip("openreview.mfa")
        import openreview

        monkeypatch.setattr(mfa, "_passkey_browser_flow", mfa._passkey_browser_flow)
        monkeypatch.setattr(cli, "token_owner", lambda client, token: (PROFILE, None, True))
        port = _free_port()
        assert install_passkey_flow(no_browser=True, port=port, username="~Test_User1")

        # token="x" is not a JWT, so the constructor makes no network calls.
        client = openreview.api.OpenReviewClient(baseurl="https://api2.example.org", token="x")
        outcome = {}

        def resolve():
            outcome["result"] = client._OpenReviewClient__resolve_passkey("pending-123")

        thread = threading.Thread(target=resolve, daemon=True)
        thread.start()
        _wait_until(lambda: _is_listening(port), what="callback server")
        status, _ = _post_callback(port, {"token": "hooked", "user": USER_JSON})
        assert status == 200
        thread.join(10)
        assert not thread.is_alive()
        assert outcome["result"] == {"token": "hooked", "user": EXPECTED_USER}
        out = capsys.readouterr().out
        assert f"callbackPort={port}" in out and "pendingToken=pending-123" in out

    def test_returns_false_without_passkey_support(self, monkeypatch):
        mfa = pytest.importorskip("openreview.mfa")
        monkeypatch.delattr(mfa, "_passkey_browser_flow")
        assert install_passkey_flow() is False

    def test_returns_false_without_mfa_module(self, monkeypatch):
        def missing(name):
            raise ImportError(name)

        monkeypatch.setattr(cli.importlib, "import_module", missing)
        assert install_passkey_flow() is False


FORUM_URL = (
    "https://openreview.net/forum?id=abc&referrer=%5BAuthor%20Console%5D"
    "(%2Fgroup%3Fid%3DV%2F2026%2FConf%2FAuthors)"
)


class TestMainPasskeyArguments:
    """Tests for the passkey-related CLI options."""

    @pytest.fixture(autouse=True)
    def no_cached_token_and_gui(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
        monkeypatch.setattr(cli, "is_headless", lambda: False)

    @pytest.fixture
    def stop_at_install(self, monkeypatch):
        """Drive main() up to install_passkey_flow and record what it received."""
        installed = {}
        monkeypatch.setattr("builtins.input", lambda *a: FORUM_URL)
        monkeypatch.setattr(cli, "get_credentials", lambda: ("user@example.org", "pass"))

        def fake_install(no_browser=None, port=None, username=None):
            installed.update(no_browser=no_browser, port=port, username=username)
            raise RuntimeError("stop before network")

        monkeypatch.setattr(cli, "install_passkey_flow", fake_install)
        return installed

    def test_rejects_out_of_range_port(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["openreview-dl", "--passkey-port", "70000"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        assert "--passkey-port must be between 0 and 65535" in capsys.readouterr().err

    def test_rejects_invalid_env_port_before_prompting(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["openreview-dl"])
        monkeypatch.setenv(cli.PASSKEY_PORT_ENV, "nope")
        monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted before validating port"))
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        assert cli.PASSKEY_PORT_ENV in capsys.readouterr().err

    def test_flag_and_port_reach_install(self, monkeypatch, stop_at_install):
        monkeypatch.setattr(sys, "argv", ["openreview-dl", "--no-browser", "--passkey-port", "4321"])
        monkeypatch.delenv(cli.PASSKEY_PORT_ENV, raising=False)
        assert cli.main() == 1  # the fake install stops main() before any network
        assert stop_at_install == {"no_browser": True, "port": 4321, "username": "user@example.org"}

    def test_env_port_used_when_flag_absent(self, monkeypatch, stop_at_install):
        monkeypatch.setattr(sys, "argv", ["openreview-dl"])
        monkeypatch.setenv(cli.PASSKEY_PORT_ENV, "5555")
        assert cli.main() == 1
        assert stop_at_install == {"no_browser": None, "port": 5555, "username": "user@example.org"}

    def test_flag_port_wins_over_env(self, monkeypatch, stop_at_install):
        monkeypatch.setattr(sys, "argv", ["openreview-dl", "--passkey-port", "0"])
        monkeypatch.setenv(cli.PASSKEY_PORT_ENV, "5555")
        assert cli.main() == 1
        assert stop_at_install["port"] == 0

    @pytest.fixture
    def login_raises(self, monkeypatch):
        """Drive main() into the login and make the client constructor raise."""
        monkeypatch.setattr(sys, "argv", ["openreview-dl"])
        monkeypatch.delenv(cli.PASSKEY_PORT_ENV, raising=False)
        monkeypatch.setattr("builtins.input", lambda *a: FORUM_URL)
        monkeypatch.setattr(cli, "get_credentials", lambda: ("user@example.org", "pass"))
        monkeypatch.setattr(cli, "install_passkey_flow", lambda **kw: True)

        def set_error(error):
            def construct(*args, **kwargs):
                raise error

            monkeypatch.setattr(cli.openreview.api, "OpenReviewClient", construct)

        return set_error

    def test_ctrl_c_during_login_exits_cleanly(self, login_raises, capsys):
        login_raises(KeyboardInterrupt())
        try:
            code = cli.main()
        except KeyboardInterrupt:
            pytest.fail("main() let KeyboardInterrupt escape")
        assert code == 130
        assert "Cancelled" in capsys.readouterr().out

    def test_mfa_failure_gets_a_readable_message(self, login_raises, capsys):
        login_raises(
            cli.openreview.openreview.OpenReviewException(
                {"name": "MfaError", "message": "Passkey authentication failed or timed out."}
            )
        )
        assert cli.main() == 1
        out = capsys.readouterr().out
        assert "multi-factor authentication did not complete" in out
        assert "{'name'" not in out


def _jwt(exp=None, user_id="~Test_User1") -> str:
    """A syntactically valid, unsigned JWT like the ones OpenReview issues."""
    import base64

    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    payload = {"user": {"id": user_id, "profile": {"id": user_id}}}
    if exp is not None:
        payload["exp"] = exp
    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(payload)}.signature"


FUTURE = int(time.time()) + 3 * 24 * 3600
PAST = int(time.time()) - 60


class TestTokenHelpers:
    """Tests for normalize_token, token_expiry, token_is_expired, describe_expiry."""

    @pytest.mark.parametrize(
        "raw",
        [
            "abc.def.ghi",
            "  abc.def.ghi\n",
            "abc.def\n.ghi",
            "Bearer abc.def.ghi",
            "bearer  abc.def.ghi",
            "'abc.def.ghi'",
            '"abc.def.ghi"',
        ],
    )
    def test_normalize_token(self, raw):
        assert cli.normalize_token(raw) == "abc.def.ghi"

    def test_expiry_from_exp_claim(self):
        expiry = cli.token_expiry(_jwt(exp=FUTURE))
        assert expiry is not None
        assert int(expiry.timestamp()) == FUTURE
        assert expiry.tzinfo is not None

    @pytest.mark.parametrize("token", ["", "garbage", "a.b", "a.!!!.c", _jwt(exp=None), "a." + "e30" + ".c"])
    def test_expiry_unknown_for_odd_tokens(self, token):
        assert cli.token_expiry(token) is None
        assert cli.token_is_expired(token) is False
        assert cli.describe_expiry(token) == "expiry unknown"

    def test_is_expired(self):
        assert cli.token_is_expired(_jwt(exp=PAST)) is True
        assert cli.token_is_expired(_jwt(exp=FUTURE)) is False

    def test_describe_expiry_mentions_the_date(self):
        from datetime import datetime

        text = cli.describe_expiry(_jwt(exp=FUTURE))
        assert text.startswith("valid until ")
        assert datetime.fromtimestamp(FUTURE).astimezone().strftime("%Y-%m-%d") in text


class TestTokenCache:
    """Tests for save_token / load_cached_token / delete_token."""

    @pytest.fixture(autouse=True)
    def isolated_config_dir(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
        self.config_dir = tmp_path

    def test_roundtrip(self, capsys):
        token = _jwt(exp=FUTURE)
        cli.save_token(token)
        assert (self.config_dir / "token.enc").read_text() != token  # encrypted
        assert cli.load_cached_token() == token
        assert "Token cached at" in capsys.readouterr().out

    def test_missing_cache_is_none(self):
        assert cli.load_cached_token() is None

    def test_expired_token_is_dropped(self, capsys):
        cli.save_token(_jwt(exp=PAST))
        assert cli.load_cached_token() is None
        assert not (self.config_dir / "token.enc").exists()
        assert "expired" in capsys.readouterr().out

    def test_unreadable_cache_is_removed(self, capsys):
        (self.config_dir / "token.enc").write_text("not encrypted")
        assert cli.load_cached_token() is None
        assert not (self.config_dir / "token.enc").exists()
        assert "Could not read the cached token" in capsys.readouterr().out

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_cache_files_are_private(self, capsys):
        import stat

        (self.config_dir / "token.enc").write_text("stale")
        (self.config_dir / "token.enc").chmod(0o644)
        cli.save_token(_jwt(exp=FUTURE))
        cli.save_credentials("user@example.org", "pw")
        for name in ("token.enc", "credentials.enc"):
            mode = stat.S_IMODE((self.config_dir / name).stat().st_mode)
            assert mode == 0o600, (name, oct(mode))

    def test_delete_credentials_removes_token_too(self, capsys):
        cli.save_token(_jwt(exp=FUTURE))
        cli.delete_credentials()
        assert not (self.config_dir / "token.enc").exists()
        assert "Cached token deleted" in capsys.readouterr().out


class _FakeOpenReviewClient:
    """Stands in for openreview.api.OpenReviewClient; records constructor kwargs."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.baseurl = kwargs.get("baseurl")
        self.token = kwargs.get("token") or ("issued.token.value" if kwargs.get("username") else None)
        self.headers = {}
        self.session = _FakeSession(_FakeResponse(200, {"profiles": [PROFILE]}))
        self.profile = type("P", (), {"id": "~Test_User1"})() if kwargs.get("username") else None
        _FakeOpenReviewClient.instances.append(self)


class _Args:
    def __init__(self, **kw):
        self.token = kw.get("token")
        self.no_browser = kw.get("no_browser", False)
        self.auth = kw.get("auth", False)


class _Tty:
    def isatty(self):
        return True


class _Pipe:
    def isatty(self):
        return False


def _fail_prompt(*args, **kwargs):
    pytest.fail("unexpected prompt")


class TestResolveClient:
    """Tests for resolve_client - token first, password login as fallback."""

    @pytest.fixture(autouse=True)
    def isolate(self, monkeypatch, tmp_path):
        _FakeOpenReviewClient.instances.clear()
        monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
        monkeypatch.setattr(cli.openreview.api, "OpenReviewClient", _FakeOpenReviewClient)
        monkeypatch.setattr(cli, "token_owner", lambda client, token: (PROFILE, None, True))
        monkeypatch.setattr(cli, "is_headless", lambda: False)
        monkeypatch.setattr(sys, "stdin", _Tty())
        monkeypatch.setattr(cli, "get_credentials", lambda: ("user@example.org", "pw"))
        self.installed = []
        monkeypatch.setattr(cli, "install_passkey_flow", lambda **kw: self.installed.append(kw) or True)
        monkeypatch.setattr("builtins.input", _fail_prompt)
        monkeypatch.setattr(cli.getpass, "getpass", _fail_prompt)
        self.config_dir = tmp_path

    def _paste(self, monkeypatch, text):
        prompts = []
        monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": prompts.append(prompt) or text)
        return prompts

    def test_token_flag_is_used_and_cached(self, capsys):
        token = _jwt(exp=FUTURE)
        client = cli.resolve_client(_Args(token=f" Bearer {token}\n"), 0)
        assert client.kwargs == {"baseurl": cli.BASEURL, "token": token}
        assert cli.load_cached_token() == token
        out = capsys.readouterr().out
        assert "Logged in as ~Test_User1 with a token" in out
        assert self.installed == []

    def test_rejected_token_flag_is_not_cached(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "token_owner", lambda c, t: (None, "OpenReview rejected it (HTTP 401)", True))
        assert cli.resolve_client(_Args(token=_jwt(exp=FUTURE)), 0) is None
        assert cli.load_cached_token() is None
        out = capsys.readouterr().out
        assert "OpenReview did not accept the token: OpenReview rejected it (HTTP 401)." in out
        assert "openreview-dl --auth" in out

    def test_expired_token_flag_is_refused_without_network(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "token_owner", _fail_prompt)
        assert cli.resolve_client(_Args(token=_jwt(exp=PAST)), 0) is None
        assert _FakeOpenReviewClient.instances == []
        out = capsys.readouterr().out
        assert "That token has expired (valid until" in out
        assert "openreview-dl --auth" in out

    def test_cached_token_is_used_without_resaving(self, capsys):
        token = _jwt(exp=FUTURE)
        cli.save_token(token)
        capsys.readouterr()
        client = cli.resolve_client(_Args(), 0)
        assert client.kwargs["token"] == token
        assert "Token cached at" not in capsys.readouterr().out

    def test_headless_uses_cached_token_without_prompting(self, monkeypatch):
        monkeypatch.setattr(cli, "is_headless", lambda: True)
        token = _jwt(exp=FUTURE)
        cli.save_token(token)
        client = cli.resolve_client(_Args(), 0)
        assert client.kwargs["token"] == token

    def test_rejected_cached_token_is_deleted(self, monkeypatch, capsys):
        cli.save_token(_jwt(exp=FUTURE))
        monkeypatch.setattr(cli, "token_owner", lambda c, t: (None, "OpenReview rejected it (HTTP 401)", True))
        assert cli.resolve_client(_Args(), 0) is None
        assert not (self.config_dir / "token.enc").exists()
        assert "Cached token deleted" in capsys.readouterr().out

    def test_network_error_keeps_cached_token(self, monkeypatch, capsys):
        token = _jwt(exp=FUTURE)
        cli.save_token(token)
        monkeypatch.setattr(
            cli, "token_owner", lambda c, t: (None, "could not reach OpenReview to verify it (X: y)", False)
        )
        assert cli.resolve_client(_Args(), 0) is None
        assert cli.load_cached_token() == token
        out = capsys.readouterr().out
        assert "Could not check the token with OpenReview: could not reach OpenReview" in out
        assert "The cached token was kept" in out
        assert "did not accept" not in out and "openreview-dl --auth" not in out

    def test_network_error_with_token_flag(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "token_owner", lambda c, t: (None, "OpenReview answered HTTP 503", False))
        assert cli.resolve_client(_Args(token=_jwt(exp=FUTURE)), 0) is None
        assert cli.load_cached_token() is None
        out = capsys.readouterr().out
        assert "Could not check the token with OpenReview: OpenReview answered HTTP 503." in out
        assert "Check the connection and try again." in out

    def test_headless_offers_hidden_paste_and_uses_it(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "is_headless", lambda: True)
        token = _jwt(exp=FUTURE)
        prompts = self._paste(monkeypatch, f"{token}\n")
        client = cli.resolve_client(_Args(), 0)
        assert client.kwargs["token"] == token
        assert len(prompts) == 1 and "Enter" in prompts[0]
        assert "openreview-dl --auth" in capsys.readouterr().out
        assert cli.load_cached_token() == token

    def test_no_browser_flag_offers_paste_too(self, monkeypatch):
        token = _jwt(exp=FUTURE)
        self._paste(monkeypatch, token)
        client = cli.resolve_client(_Args(no_browser=True), 0)
        assert client.kwargs["token"] == token

    def test_headless_enter_falls_back_to_password_login(self, monkeypatch):
        monkeypatch.setattr(cli, "is_headless", lambda: True)
        self._paste(monkeypatch, "   ")
        client = cli.resolve_client(_Args(no_browser=True), 4321)
        assert client.kwargs == {
            "baseurl": cli.BASEURL,
            "username": "user@example.org",
            "password": "pw",
            "tokenExpiresIn": None,
        }
        assert self.installed == [{"no_browser": True, "port": 4321, "username": "user@example.org"}]

    def test_headless_without_terminal_skips_the_prompt(self, monkeypatch):
        monkeypatch.setattr(cli, "is_headless", lambda: True)
        monkeypatch.setattr(sys, "stdin", _Pipe())
        client = cli.resolve_client(_Args(), 0)
        assert client.kwargs["username"] == "user@example.org"

    def test_gui_machine_goes_straight_to_password_login(self):
        client = cli.resolve_client(_Args(), 0)
        assert client.kwargs["username"] == "user@example.org"
        assert self.installed == [{"no_browser": None, "port": 0, "username": "user@example.org"}]


class TestAuthCommand:
    """Tests for auth_command / --auth."""

    @pytest.fixture(autouse=True)
    def isolate(self, monkeypatch, tmp_path):
        _FakeOpenReviewClient.instances.clear()
        monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
        monkeypatch.setattr(cli.openreview.api, "OpenReviewClient", _FakeOpenReviewClient)
        monkeypatch.setattr(cli, "get_credentials", lambda: ("user@example.org", "pw"))
        monkeypatch.setattr(cli, "install_passkey_flow", lambda **kw: True)
        monkeypatch.setattr("builtins.input", _fail_prompt)
        monkeypatch.delenv(cli.PASSKEY_PORT_ENV, raising=False)

    def test_logs_in_with_max_lifetime_and_prints_token(self, capsys):
        assert cli.auth_command(_Args(), 0) == 0
        client = _FakeOpenReviewClient.instances[0]
        assert client.kwargs["username"] == "user@example.org"
        assert client.kwargs["tokenExpiresIn"] == cli.AUTH_TOKEN_LIFETIME == 7 * 24 * 3600
        out = capsys.readouterr().out
        assert "Logged in as ~Test_User1" in out
        assert "\nissued.token.value\n" in out
        assert "--token TOKEN" in out and "shell history" in out
        for line in out.splitlines():
            if line != "issued.token.value":
                assert len(line) <= 80, line

    def test_empty_credentials_produce_no_token(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "get_credentials", lambda: ("", ""))
        assert cli.auth_command(_Args(), 0) == 1
        out = capsys.readouterr().out
        assert "did not produce a token" in out
        assert "None" not in out

    def test_main_routes_auth_without_asking_for_a_url(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["openreview-dl", "--auth"])
        assert cli.main() == 0
        assert "issued.token.value" in capsys.readouterr().out

    def test_main_reports_login_errors_through_the_handler(self, monkeypatch, capsys):
        def failing(**kwargs):
            raise cli.openreview.openreview.OpenReviewException(
                {"name": "Error", "message": "Invalid username or password"}
            )

        monkeypatch.setattr(cli.openreview.api, "OpenReviewClient", failing)
        monkeypatch.setattr("builtins.input", lambda *a: "n")
        monkeypatch.setattr(sys, "argv", ["openreview-dl", "--auth"])
        assert cli.main() == 1
        out = capsys.readouterr().out
        assert "Error: Invalid username or password." in out
        assert "Cached credentials were not deleted." in out
        assert "An unexpected error occurred" not in out


class TestMainTokenRouting:
    """--token and --wipe-credentials through main()."""

    @pytest.fixture(autouse=True)
    def isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
        monkeypatch.delenv(cli.PASSKEY_PORT_ENV, raising=False)
        self.config_dir = tmp_path

    def test_token_flag_reaches_resolve_client(self, monkeypatch, capsys):
        seen = {}

        def fake_resolve(args, passkey_port):
            seen.update(token=args.token, port=passkey_port)
            return None

        monkeypatch.setattr(sys, "argv", ["openreview-dl", "--token", "abc.def.ghi", "--passkey-port", "7"])
        monkeypatch.setattr("builtins.input", lambda *a: FORUM_URL)
        monkeypatch.setattr(cli, "resolve_client", fake_resolve)
        assert cli.main() == 1
        assert seen == {"token": "abc.def.ghi", "port": 7}
        assert "An unexpected error occurred" not in capsys.readouterr().out

    def test_wipe_removes_token(self, monkeypatch, capsys):
        cli.save_token(_jwt(exp=FUTURE))
        (self.config_dir / "credentials.enc").write_text("x")
        monkeypatch.setattr(sys, "argv", ["openreview-dl", "--wipe-credentials"])
        assert cli.main() == 0
        assert not (self.config_dir / "token.enc").exists()
        assert not (self.config_dir / "credentials.enc").exists()
        out = capsys.readouterr().out
        assert "Cached credentials deleted" in out and "Cached token deleted" in out
