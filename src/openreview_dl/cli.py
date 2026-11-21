import argparse
import base64
import functools
import getpass
import glob
import http.server
import importlib
import json
import os
import platform
import re
import socket
import socketserver
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from operator import itemgetter
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import openreview
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import discover, picker

def get_config_dir() -> Path:
    """Get the config directory path, creating it if it doesn't exist."""
    config_dir = Path.home() / ".config" / "openreview-dl"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return config_dir


def write_private(path: Path, text: str):
    """Write ``text`` to ``path`` so that only the current user can read it."""
    try:
        os.chmod(path, 0o600)  # tighten a pre-existing file before writing secrets into it
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _discard(path: Path, reason: str):
    """Remove a cache file that can never become useful again; never raises."""
    print(f"{reason}; removing {path}.")
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        print(f"Could not remove it ({e}); it will be ignored.")


class CacheError(Exception):
    """The cache key or a cache file cannot be used (message says why)."""


def get_credentials_path() -> Path:
    """Get the path to the credentials file."""
    return get_config_dir() / "credentials.enc"


def get_key_path() -> Path:
    """Path of the random key that encrypts the cached credentials and token."""
    return get_config_dir() / "key"


def legacy_key(hostname=None) -> bytes:
    """The key versions before 0.4 derived from the hostname.

    Kept only to read files written by those versions. The hostname is a poor
    key source: on macOS it changes with the network (DHCP/reverse DNS, or a
    ``-2`` suffix after an mDNS name clash), which made cached logins
    undecryptable until wiped.
    """
    salt = b"fixed_salt_for_openreview"
    if hostname is None:
        hostname = platform.uname().node
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(hostname.encode()))


def _legacy_hostnames():
    """Hostname spellings an older version may have used for its key.

    Besides the current name this tries the bare and ``.local`` forms, the
    mDNS clash variants (``name-2.local`` while the file was written as
    ``name.local``, or the reverse) and, on macOS, the LocalHostName that the
    machine falls back to when no network supplies a name.
    """
    node = platform.uname().node
    short = node.split(".")[0]
    base = re.sub(r"-\d+$", "", short)
    stems = [short, base, f"{base}-2", f"{base}-3"]
    if sys.platform == "darwin":
        try:
            local = subprocess.run(
                ["scutil", "--get", "LocalHostName"],
                capture_output=True, text=True, timeout=2, check=False,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            local = ""
        if local:
            stems.append(local)
    candidates = [node]
    for stem in stems:
        candidates += [stem, stem + ".local"]
    return [h for i, h in enumerate(candidates) if h and h not in candidates[:i]]


def get_key() -> bytes:
    """A random key kept in the config dir, created on first use.

    Unlike the hostname-derived key it does not change when the machine moves
    to another network, so cached logins stay readable. Raises CacheError when
    the key file exists but cannot be read or written.
    """
    path = get_key_path()
    try:
        key = path.read_bytes().strip()
        Fernet(key)  # raises ValueError on a malformed key
        return key
    except FileNotFoundError:
        pass
    except ValueError:
        print(f"The key file at {path} is malformed; replacing it.")
    except OSError as e:
        raise CacheError(f"Cannot read the key file at {path}: {e}") from e
    key = Fernet.generate_key()
    try:
        write_private(path, key.decode())
    except OSError as e:
        raise CacheError(f"Cannot write the key file at {path}: {e}") from e
    return key


def encrypt(text):
    f = Fernet(get_key())
    return f.encrypt(text.encode()).decode()


def decrypt(text):
    f = Fernet(get_key())
    return f.decrypt(text.encode()).decode()


def read_secret(path: Path, what: str):
    """Decrypt the cached file at ``path``, or return None if there is nothing usable.

    Files written by older versions (hostname-derived key) are re-encrypted
    with the stable key on first read. A file that cannot be decrypted is left
    in place (it may decrypt under another hostname later, and a new login
    overwrites it anyway); the caller falls back to prompting. Never raises.
    """
    if not path.exists():
        return None
    try:
        data = path.read_bytes().decode("ascii").strip()  # Fernet tokens are base64
    except UnicodeDecodeError:
        _discard(path, f"The cached {what} at {path} is not an encrypted file")
        return None
    except OSError as e:
        print(f"Could not read the cached {what} at {path}: {e}")
        return None
    try:
        return decrypt(data)
    except (InvalidToken, ValueError):
        pass
    except CacheError as e:
        print(f"{e}. Continuing without the cached {what}.")
        return None
    for hostname in _legacy_hostnames():
        try:
            text = Fernet(legacy_key(hostname)).decrypt(data.encode()).decode()
        except (InvalidToken, ValueError):
            continue
        try:
            write_private(path, encrypt(text))  # migrate to the stable key
        except (OSError, CacheError) as e:
            print(f"Could not re-encrypt the cached {what} at {path}: {e}")
        return text
    print(
        f"Could not decrypt the cached {what} at {path} (saved under a different "
        "hostname by an older version, or with a key that no longer exists). "
        f"You will be asked to log in again; caching again replaces it."
    )
    return None


def delete_credentials():
    creds_path = get_credentials_path()
    if creds_path.exists():
        creds_path.unlink()
        print(f"Cached credentials deleted from: {creds_path}")
    else:
        print("No cached credentials found.")
    delete_token()
    get_key_path().unlink(missing_ok=True)


def load_cached_credentials():
    creds_path = get_credentials_path()
    text = read_secret(creds_path, "credentials")
    if text is None:
        return None, None
    try:
        creds = json.loads(text)
        return creds["username"], creds["password"]
    except (ValueError, KeyError, TypeError):
        _discard(creds_path, f"The cached credentials at {creds_path} are malformed")
        return None, None


def save_credentials(username, password):
    creds = {"username": username, "password": password}
    creds_path = get_credentials_path()
    try:
        write_private(creds_path, encrypt(json.dumps(creds)))
    except (OSError, CacheError) as e:
        print(f"Could not cache the credentials: {e}")
        return
    print(f"Credentials cached at: {creds_path}")


def get_credentials():
    username, password = load_cached_credentials()
    if username and password:
        print(f"Using cached credentials from: {get_credentials_path()}")
        return username, password

    username = input("Enter your OpenReview username: ")
    password = getpass.getpass("Enter your OpenReview password: ")

    cache_choice = input(
        "Do you want to cache these credentials? Note that this is not fully secure. (y/N): "
    ).lower()
    if cache_choice == "y":
        save_credentials(username, password)

    return username, password

# ---------------------------------------------------------------------------
# Login tokens
#
# ``openreview-dl --auth`` logs in on a machine that has a browser and prints
# the resulting OpenReview session token. Pasting that token on a machine
# without a browser skips the password + MFA login entirely. Tokens are JWTs
# with an ``exp`` claim; the API issues them for at most one week.
# ---------------------------------------------------------------------------

BASEURL = "https://api2.openreview.net"
AUTH_TOKEN_LIFETIME = 7 * 24 * 3600  # longest the OpenReview API allows


def get_token_path() -> Path:
    """Path of the cached login token (encrypted like the credentials)."""
    return get_config_dir() / "token.enc"


def normalize_token(text: str) -> str:
    """Clean a pasted token: drop whitespace and newlines, quotes, a Bearer prefix."""
    token = "".join(text.split())
    if token.lower().startswith("bearer"):
        token = token[len("bearer"):]
    return token.strip("\"'")


def _jwt_payload(token: str):
    """Decode a JWT's payload without verifying it; None if it is not a JWT."""
    try:
        payload = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError, TypeError, AttributeError):
        return None
    return payload if isinstance(payload, dict) else None


def token_expiry(token: str):
    """Expiry of a JWT from its ``exp`` claim as a UTC datetime, or None if unknown."""
    exp = (_jwt_payload(token) or {}).get("exp")
    try:
        return datetime.fromtimestamp(float(exp), tz=timezone.utc) if exp else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _jwt_owner_id(token: str):
    """The profile id (or login id) an OpenReview session token was issued to."""
    payload = _jwt_payload(token) or {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else payload
    profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
    owner = profile.get("id") or user.get("id")
    return owner if isinstance(owner, str) and owner else None


def token_is_expired(token: str, now=None) -> bool:
    expiry = token_expiry(token)
    return expiry is not None and expiry <= (now or datetime.now(timezone.utc))


def describe_expiry(token: str) -> str:
    expiry = token_expiry(token)
    if expiry is None:
        return "expiry unknown"
    return f"valid until {expiry.astimezone().strftime('%Y-%m-%d %H:%M %Z')}"


def save_token(token: str):
    path = get_token_path()
    try:
        write_private(path, encrypt(token))
    except (OSError, CacheError) as e:
        print(f"Could not cache the token: {e}")
        return
    print(f"Token cached at: {path} ({describe_expiry(token)})")


def load_cached_token():
    """The cached token if present and not expired, else None (expired ones are removed)."""
    path = get_token_path()
    token = read_secret(path, "token")
    if token is None:
        return None
    if token_is_expired(token):
        _discard(path, f"The cached token expired ({describe_expiry(token)})")
        return None
    return token


def delete_token():
    path = get_token_path()
    if path.exists():
        try:
            path.unlink()
        except OSError as e:
            print(f"Could not delete the cached token at {path}: {e}")
            return
        print(f"Cached token deleted from: {path}")


NEW_TOKEN_HINT = "Run `openreview-dl --auth` on a machine with a browser to get a new one."


def print_token_handoff(token: str, profile_id):
    print(f"\nLogged in as {profile_id}. Token {describe_expiry(token)}:\n")
    print(token)
    print("\nOn the machine without a browser, run openreview-dl and paste this token at")
    print("its prompt (input is hidden). --token TOKEN also works but ends up in your")
    print("shell history. The token is cached there (readable only by you) until it")
    print("expires; --wipe-credentials removes it.")


# ---------------------------------------------------------------------------
# Passkey (WebAuthn) multi-factor authentication
#
# openreview-py >= 2.0 resolves passkey MFA by starting a local HTTP server,
# opening ``{baseurl}/mfa/webauthn-auth`` in a browser and waiting for that page
# to POST the login token back to ``http://127.0.0.1:<port>``. It never prints
# the URL, so on a headless machine (SSH session, container, CI) the login just
# hangs until it times out. The functions below replace that flow with one that
# prints the URL, skips the browser when none can be reached, explains the SSH
# port forward needed for the callback to arrive, and only accepts a token that
# OpenReview confirms belongs to the account being logged in.
# ---------------------------------------------------------------------------

NO_BROWSER_ENV = "OPENREVIEW_DL_NO_BROWSER"
PASSKEY_PORT_ENV = "OPENREVIEW_DL_PASSKEY_PORT"
PASSKEY_TIMEOUT = 120
HEADLESS_PASSKEY_TIMEOUT = 300
# Seconds a single callback connection may stay silent before it is dropped, so
# an idle probe (nc, a browser preconnect) cannot stall the wait past the deadline.
PASSKEY_CONNECTION_TIMEOUT = 10
_MAX_CALLBACK_BODY = 1 << 20

_TRUTHY = ("1", "true", "yes", "on")


def _html_page(title: str, heading: str, text: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head>"
        "<body style='font-family:sans-serif;text-align:center;padding:3em'>"
        f"<h1>{heading}</h1><p>{text}</p></body></html>"
    )


_PASSKEY_SUCCESS_PAGE = _html_page(
    "Authentication Complete",
    "&#10003; Authentication complete",
    "You may close this tab and return to your terminal.",
)
_PASSKEY_WAITING_PAGE = _html_page(
    "Waiting for passkey",
    "Waiting for passkey authentication",
    "This is the openreview-dl callback address. Complete the passkey login on "
    "the OpenReview page printed in your terminal.",
)
_PASSKEY_REJECTED_PAGE = _html_page(
    "Login not accepted",
    "Login not accepted",
    "openreview-dl could not verify that the login token it received belongs "
    "to the account it is logging in to, so it was ignored. Check the terminal "
    "for details.",
)


def env_flag(name: str) -> bool:
    """Return True if the environment variable is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def is_headless() -> bool:
    """Return True when no browser can be opened for the current user.

    ``OPENREVIEW_DL_NO_BROWSER`` forces headless mode. An SSH session always
    counts as headless: even with X forwarding, a browser started on this
    machine cannot reach the passkey on the user's own device. Otherwise a
    Linux/BSD browser needs a display or an explicit ``BROWSER`` command (as on
    WSL), while macOS and Windows always have a GUI session.
    """
    if env_flag(NO_BROWSER_ENV):
        return True
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    if sys.platform.startswith(("linux", "freebsd", "openbsd", "netbsd")):
        return not (
            os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("BROWSER")
        )
    return False


def get_passkey_port() -> int:
    """Callback port from ``OPENREVIEW_DL_PASSKEY_PORT``; 0 means pick a free one."""
    value = os.environ.get(PASSKEY_PORT_ENV, "").strip()
    if not value:
        return 0
    try:
        port = int(value)
    except ValueError:
        raise ValueError(f"{PASSKEY_PORT_ENV} must be an integer, got {value!r}")
    if not 0 <= port <= 65535:
        raise ValueError(f"{PASSKEY_PORT_ENV} must be between 0 and 65535, got {port}")
    return port


def build_passkey_url(baseurl: str, mfa_pending_token: str, port: int) -> str:
    """URL of the OpenReview WebAuthn page that posts the token back to ``port``."""
    return (
        f"{baseurl.rstrip('/')}/mfa/webauthn-auth"
        f"?pendingToken={quote(mfa_pending_token, safe='')}"
        f"&callbackPort={port}"
    )


def _get_profile(client, token: str, **params):
    """``GET /profiles`` with ``token`` as bearer; ``(profile, None, True)`` or ``(None, reason, definitive)``."""
    try:
        response = client.session.get(
            f"{client.baseurl.rstrip('/')}/profiles",
            params=params,
            headers={**client.headers, "Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except Exception as e:  # network trouble, not a verdict on the token
        return None, f"could not reach OpenReview to verify it ({type(e).__name__}: {e})", False
    if response.status_code == 401:
        return None, "OpenReview rejected it (HTTP 401)", True
    if response.status_code == 404:
        return None, "OpenReview knows no such profile", True
    if response.status_code != 200:
        return None, f"OpenReview answered HTTP {response.status_code}", False
    try:
        profiles = response.json().get("profiles") or []
    except Exception:
        return None, "OpenReview sent an unreadable reply", False
    if profiles and isinstance(profiles[0], dict) and profiles[0].get("id"):
        return profiles[0], None, True
    return None, "OpenReview returned no profile", True


def token_owner(client, token: str):
    """Ask OpenReview whose session ``token`` is.

    An OpenReview token is a JWT naming the account it was issued to. Fetching
    that profile with the token as bearer (what openreview-py's constructor
    does for ``token=``) both validates the token, since the server checks the
    signature over the whole token and so over that claim, and returns the
    owner's profile. Returns ``(profile, None, True)`` on success, otherwise
    ``(None, reason, definitive)``: ``definitive`` is True only when OpenReview
    itself turned the token down, not when the check could not be completed.
    """
    owner_id = _jwt_owner_id(token)
    if not owner_id:
        return None, "it is not an OpenReview session token", True
    if owner_id.startswith("~"):
        return _get_profile(client, token, id=owner_id)
    return _get_profile(client, token, email=owner_id.lower())


def _profile_matches_username(profile: dict, username) -> bool:
    """True if ``username`` (the email or ~id used to log in) identifies ``profile``."""
    if not username:
        return True
    wanted = username.strip().lower()
    content = profile.get("content") or {}
    candidates = [profile.get("id"), content.get("preferredEmail")]
    candidates += content.get("emails") or []
    candidates += content.get("emailsConfirmed") or []
    candidates += [
        name.get("username")
        for name in content.get("names") or []
        if isinstance(name, dict)
    ]
    return wanted in {str(c).strip().lower() for c in candidates if c}


def _token_matches_username(client, token: str, profile: dict, username) -> bool:
    """True if ``username`` (the email or ~id used to log in) is ``profile``'s account.

    Falls back to asking OpenReview which profile owns an email address, since a
    profile's emails may not be listed in the fetched content.
    """
    if not username or _profile_matches_username(profile, username):
        return True
    wanted = username.strip().lower()
    if wanted.startswith("~") or "@" not in wanted:
        return False
    other, _, _ = _get_profile(client, token, email=wanted)
    return bool(other) and str(other.get("id")).lower() == str(profile.get("id")).lower()


def _callback_user(raw_user, profile: dict) -> dict:
    """The ``user`` object openreview-py expects, rebuilt from ``profile`` if needed."""
    try:
        user = json.loads(raw_user) if raw_user else None
    except (ValueError, RecursionError):  # not JSON, or absurdly nested
        user = None
    if (
        isinstance(user, dict)
        and isinstance(user.get("profile"), dict)
        and user["profile"].get("id")
    ):
        return user
    return {"id": profile.get("id"), "profile": profile}


class _PasskeyCallbackHandler(http.server.BaseHTTPRequestHandler):
    """Receives the form POST the OpenReview WebAuthn page sends after login."""

    timeout = PASSKEY_CONNECTION_TIMEOUT

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= _MAX_CALLBACK_BODY:
            self.send_response(400)
            self.end_headers()
            return
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = parse_qs(body)
        token = parsed.get("token", [None])[0]
        if not token:
            self.send_response(400)
            self.end_headers()
            return

        # Any local process can reach this port, so do not end the wait (and
        # free the port for someone else to grab) on an unverified token.
        server = self.server
        profile, reason, _ = token_owner(server.client, token)
        if profile is None:
            self._reject(f"Ignoring a passkey callback: {reason}.")
            return
        if not _token_matches_username(server.client, token, profile, server.username):
            self._reject(
                f"Ignoring a passkey callback for {profile.get('id')}: "
                f"logging in as {server.username}."
            )
            return

        # Build the whole result before publishing it: the wait loop ends as
        # soon as "token" appears.
        user = _callback_user(parsed.get("user", [None])[0], profile)
        server.result.update(token=token, user=user)
        self._send_page(_PASSKEY_SUCCESS_PAGE)

    def _reject(self, message: str):
        print(message)
        print("Sign in with your passkey on the OpenReview page again; still waiting.")
        self._send_page(_PASSKEY_REJECTED_PAGE, status=403)

    def do_GET(self):
        # Someone opened the callback address directly (e.g. to test a port
        # forward). Answer instead of consuming the wait with an error.
        self._send_page(_PASSKEY_WAITING_PAGE)

    def _send_page(self, page: str, status: int = 200):
        body = page.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


class _PasskeyCallbackServer(http.server.HTTPServer):
    # Let a fixed port be reused right after a previous run left it in
    # TIME_WAIT. Not on Windows, where SO_REUSEADDR would also allow binding a
    # port that another socket is still listening on.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, port: int, client, username=None):
        super().__init__(("127.0.0.1", port), _PasskeyCallbackHandler)
        self.client = client
        self.username = username
        self.result = {}

    def server_bind(self):
        # HTTPServer.server_bind() reverse-resolves the bound address with
        # socket.getfqdn() between bind() and listen(). Resolving 127.0.0.1 can
        # stall for ~30 s on macOS (seen with python.org builds on GitHub's
        # runners), during which the port refuses connections and the login
        # URL has not been printed yet. Only CGI handlers read server_name, so
        # record the address without a lookup.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def _ssh_target_guess() -> str:
    """``user@host`` for the example ssh command; placeholders if unknown."""
    try:
        user = getpass.getuser()
    except Exception:  # no passwd entry for this UID, e.g. docker run --user
        user = "USER"
    try:
        host = socket.gethostname() or "HOST"
    except Exception:
        host = "HOST"
    return f"{user}@{host}"


def _print_port_forward_instructions(port: int, fixed_port: bool = False):
    print(f"That page posts the token to 127.0.0.1:{port} on the browser's machine, so first")
    print("forward the port to this machine (use your usual user@host or ssh alias):")
    print(f"\n  ssh -L {port}:127.0.0.1:{port} {_ssh_target_guess()}\n")
    if not fixed_port:
        print(f"Pass --passkey-port {port} (or set {PASSKEY_PORT_ENV}) to reuse it.")


def _print_headless_passkey_help(url: str, port: int, fixed_port: bool):
    print("\nNo browser is available here.\n")
    print("Easiest: press Ctrl-C, run `openreview-dl --auth` on a machine with a browser,")
    print("then run openreview-dl here again and paste the token it prints.\n")
    print("Or open this URL in a browser on another machine:")
    print(f"\n  {url}\n")
    _print_port_forward_instructions(port, fixed_port)


def passkey_browser_flow(
    client,
    mfa_pending_token,
    timeout=None,
    *,
    no_browser=None,
    port=None,
    username=None,
):
    """Complete passkey MFA and return ``{'token': ..., 'user': {...}}`` or None.

    Drop-in replacement for ``openreview.mfa._passkey_browser_flow``. The URL is
    always printed. A browser is only opened when one is likely to be reachable;
    otherwise (or when ``no_browser`` is set) the ``--auth`` token handoff and
    SSH port-forwarding instructions are printed instead. ``port`` fixes the local callback port (0 = any free).
    ``username`` is the account being logged in; a token for any other account
    is rejected.
    """
    headless = is_headless() if no_browser is None else bool(no_browser)
    if port is None:
        port = get_passkey_port()
    if timeout is None:
        timeout = HEADLESS_PASSKEY_TIMEOUT if headless else PASSKEY_TIMEOUT
    fixed_port = bool(port)

    try:
        server = _PasskeyCallbackServer(port, client, username)
    except OSError as e:
        print(f"Could not listen on 127.0.0.1:{port} for the passkey callback: {e}")
        if fixed_port:
            print("Pick another port with --passkey-port, or drop the flag and unset")
            print(f"{PASSKEY_PORT_ENV} to use any free port.")
        return None

    try:
        port = server.server_address[1]
        url = build_passkey_url(client.baseurl, mfa_pending_token, port)

        if headless:
            _print_headless_passkey_help(url, port, fixed_port)
        else:
            print("\nOpening your browser for passkey authentication. If it does not open,")
            print(f"visit this URL yourself:\n\n  {url}\n")
            if not webbrowser.open(url):
                print("Could not open a browser automatically.")
                _print_port_forward_instructions(port, fixed_port)
        print(f"\nWaiting up to {timeout:g} seconds for the passkey login to complete...")

        deadline = time.monotonic() + timeout
        while "token" not in server.result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()

    token = server.result.get("token")
    if not token:
        print("Timed out waiting for the passkey login.")
        return None
    print("Passkey login received.")
    return {"token": token, "user": server.result["user"]}


def install_passkey_flow(no_browser=None, port=None, username=None) -> bool:
    """Route openreview-py's passkey MFA through :func:`passkey_browser_flow`.

    Both OpenReview API clients look ``_passkey_browser_flow`` up on the
    ``openreview.mfa`` module at call time, so replacing the attribute is enough.
    Returns False (leaving the library untouched) on versions without passkey
    support.
    """
    try:
        mfa = importlib.import_module("openreview.mfa")
    except ImportError:
        return False
    if not hasattr(mfa, "_passkey_browser_flow"):
        return False
    mfa._passkey_browser_flow = functools.partial(
        passkey_browser_flow, no_browser=no_browser, port=port, username=username
    )
    return True


def get_unique_filename(base_filename):
    counter = 1
    filename, extension = os.path.splitext(base_filename)
    while os.path.exists(base_filename):
        base_filename = f"{filename}_{counter}{extension}"
        counter += 1
    return base_filename


def parse_openreview_url(url):
    parsed_url = urlparse(url)
    query_params = parse_qs(parsed_url.query)

    forum_id = query_params.get("id", [None])[0]

    referrer = query_params.get("referrer", [None])[0]
    if referrer:
        decoded_referrer = urllib.parse.unquote(referrer)
        venue_match = re.search(r"/group\?id=([^#]+)", decoded_referrer)
        venue_id = venue_match.group(1) if venue_match else None
    else:
        venue_id = None

    return forum_id, venue_id


def extract_reviewer_id(signature):
    # Reviewer_<id> : NeuRIPS 2024
    # Program_Committee_<id> : AAAI25
    match = re.search(r"(Reviewer|Program_Committee)_(\w+)$", signature[0])
    return match.group(2) if match else None


def _first_signature(note) -> str:
    signatures = note.get("signatures") or [""]
    return signatures[0] if isinstance(signatures[0], str) else ""


def _note_kind(note) -> str:
    """What a note is, from its invitation: "Official Review", "Decision", ..."""
    invitations = note.get("invitations") or [note.get("invitation")]
    first = invitations[0] if invitations and isinstance(invitations[0], str) else ""
    return first.split("/-/")[-1].replace("_", " ").strip() or "Note"


def _signer_label(signature: str) -> str:
    """Readable signer: "Reviewer abcd", "Program Chairs", "Authors"."""
    last = signature.split("/")[-1]
    try:
        label = openreview.tools.pretty_id(last)
    except Exception:
        label = None
    return label or last.replace("_", " ").strip() or "Unknown"


def _created(note):
    # The id breaks ties, so the output does not depend on the order of the input.
    created = note.get("cdate")
    return (created if isinstance(created, (int, float)) else 0, str(note.get("id")))


def generate_markdown(notes, include_all=False):
    """Render a forum (the submission note and its replies) as Markdown.

    By default only the reviewers' threads are included, each under the
    reviewer's anonymous id. With ``include_all`` every other note follows as
    well (meta-reviews, decisions, comments by authors or chairs), in a stable
    order, so nothing a reviewer or area chair can see is left out.
    """
    notes = [note for note in notes if isinstance(note, dict) and note.get("id")]
    # Separate the full paper and other notes
    full_paper = next((note for note in notes if note.get("replyto") is None), None)
    other_notes = [note for note in notes if note is not full_paper]
    if full_paper:
        root_id = full_paper["id"]
    else:  # the submission itself is not readable: hang the replies off its id
        root_id = next((note.get("forum") for note in other_notes if note.get("forum")), None)

    markdown_text = ""

    # Process full paper
    if full_paper:
        markdown_text += "# Full Paper\n\n"
        # A reviewer sees an anonymous paper: no "authors missing" warning then.
        markdown_text += process_full_paper(full_paper, warn=not include_all)

    # Create a dictionary to store notes by their ID
    notes_by_id = {note["id"]: note for note in other_notes}
    top_level = [note for note in other_notes if note.get("replyto") == root_id]
    if include_all:
        top_level.sort(key=_created)

    # Group top-level reviews by reviewer
    reviewer_groups = {}
    for note in top_level:
        reviewer_id = extract_reviewer_id([_first_signature(note)])
        if reviewer_id:
            reviewer_groups.setdefault(reviewer_id, []).append(note)

    seen = set()

    # Process reviewer notes and their replies
    for reviewer_id, reviewer_notes in reviewer_groups.items():
        heading = reviewer_id
        if include_all:  # "Reviewer abcd", "Ethics Reviewer abcd", ...
            heading = _signer_label(_first_signature(reviewer_notes[0]))
        markdown_text += f"## {heading}\n\n"
        for note in sorted(reviewer_notes, key=_created):
            markdown_text += process_note_thread(
                note["id"], notes_by_id, seen=seen, detailed=include_all
            )

    if include_all:
        sections = {}
        for note in top_level:
            if note["id"] not in seen:
                label = _signer_label(_first_signature(note))
                sections.setdefault(label, []).append(note)
        for label, section_notes in sections.items():
            markdown_text += f"## {label}\n\n"
            for note in section_notes:
                markdown_text += process_note_thread(
                    note["id"], notes_by_id, seen=seen, detailed=True
                )
        # Replies whose parent is not readable would otherwise vanish.
        orphans = [
            note
            for note in sorted(other_notes, key=_created)
            if note["id"] not in seen and note.get("replyto") not in notes_by_id
        ]
        # ...and so would notes that reply to themselves or to each other.
        orphans += [n for n in sorted(other_notes, key=_created) if n.get("replyto") in notes_by_id]
        rest = ""
        for note in orphans:
            rest += process_note_thread(note["id"], notes_by_id, seen=seen, detailed=True)
        if rest:
            markdown_text += "## Other notes\n\n" + rest

    return markdown_text.strip()


def process_note_thread(note_id, notes_by_id, depth=0, seen=None, detailed=False):
    if seen is None:
        seen = set()
    if note_id in seen:  # never render a note twice (or loop on bad data)
        return ""
    seen.add(note_id)
    note = notes_by_id[note_id]

    # Process the current note. Replies get a deeper heading level; indenting
    # them instead would turn their headings into plain text or code blocks.
    markdown_text = process_note(
        note,
        is_rebuttal="Authors" in _first_signature(note),
        depth=depth,
        detailed=detailed,
    )
    parent = notes_by_id.get(note.get("replyto"))
    if detailed and depth >= 3 and parent is not None:
        # Headings stop at level six, so say what a deep reply answers.
        answered = f"*In reply to {_signer_label(_first_signature(parent))} (ID: {parent['id']})*"
        markdown_text = markdown_text.replace("\n\n", f"\n\n{answered}\n\n", 1)

    # Process replies to this note
    replies = [n for n in notes_by_id.values() if n.get("replyto") == note_id]
    replies.sort(key=_created)

    for reply in replies:
        markdown_text += process_note_thread(
            reply["id"], notes_by_id, depth + 1, seen=seen, detailed=detailed
        )

    return markdown_text


def format_value(value) -> str:
    """Render a note field for Markdown, whatever shape the venue's schema gives it.

    OpenReview fields are usually strings or lists of strings, but some venues
    use structured values (e.g. a list of ``{"value": ..., "label": ...}``
    dicts). Never raises on the shape of the data.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("value", "name", "fullname", "label", "id"):
            if isinstance(value.get(key), str):
                return value[key]
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple, set)):
        return ", ".join(format_value(item) for item in value)
    return str(value)


def _field_value(field):
    """The ``value`` of an API v2 content field, or the field itself for v1 data."""
    if isinstance(field, dict) and "value" in field:
        return field["value"]
    return field


def process_full_paper(paper, warn=True):
    markdown_text = f"### Paper ID: {paper['id']}\n\n"

    content = paper.get("content") or {}
    # Essential fields that should always be present
    essential_fields = ["title", "authors", "abstract"]

    # Additional fields to check for
    additional_fields = [
        "keywords",
        "primary_keywords",
        "secondary_keywords",
        "TLDR",
        "venue",
        "paperhash",
    ]

    for field in essential_fields:
        if field in content:
            markdown_text += (
                f"**{field.capitalize()}:** {format_value(_field_value(content[field]))}\n\n"
            )
        elif warn:
            print(f"Warning: Essential field '{field}' is missing.")

    for field in additional_fields:
        if field in content:
            markdown_text += (
                f"**{field.capitalize()}:** {format_value(_field_value(content[field]))}\n\n"
            )

    markdown_text += "---\n\n"
    return markdown_text


def process_note(note, is_rebuttal=False, depth=0, detailed=False):
    heading = "#" * min(3 + depth, 6)
    if detailed:
        title = (
            f"{_note_kind(note)} by {_signer_label(_first_signature(note))} "
            f"(Note {note.get('number')}, ID: {note['id']})"
        )
    else:
        title = f"{'Rebuttal to ' if is_rebuttal else ''}Note {note.get('number')} (ID: {note['id']})"
    markdown_text = f"{heading} {title}\n\n"

    content = note.get("content") or {}
    for key, value in content.items():
        if isinstance(value, dict) and "value" in value:
            markdown_text += f"**{key.capitalize()}:**\n {format_value(value['value'])}\n\n"

    markdown_text += "---\n\n"
    return markdown_text


def ensure_output_dir(output_dir: str) -> Path:
    """Create output directory if it doesn't exist and return Path object."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def handle_openreview_error(e) -> int:
    """Explain an OpenReviewException raised while logging in or downloading."""
    message = str(e)
    if "ForbiddenError" in message:
        print("Error: You don't have permission to access this venue or paper.")
        print("This could be because:")
        print("1. You're not logged in with the correct account.")
        print("2. You don't have the necessary permissions for this venue.")
        print("3. The paper or venue ID might be incorrect.")
        print("\nPlease check your credentials and the URL, then try again.")

        delete_choice = input(
            "Would you like to delete the cached credentials? (y/N): "
        ).lower()
        if delete_choice == "y":
            delete_credentials()
        else:
            print("Cached credentials were not deleted.")
    elif "MfaError" in message or "MfaRequiredError" in message:
        print(
            "Error: multi-factor authentication did not complete "
            "(see the messages above)."
        )
    elif "Invalid username or password" in message:
        print("Error: Invalid username or password.")
        delete_choice = input(
            "Would you like to delete the cached credentials? (y/N): "
        ).lower()
        if delete_choice == "y":
            delete_credentials()
            print("Please run the script again and enter your credentials.")
        else:
            print("Cached credentials were not deleted.")
    else:
        print(f"An OpenReview error occurred: {e}")
    return 1


def password_login(args, passkey_port, expires_in=None):
    """Log in with username/password (cached or prompted), MFA included."""
    username, password = get_credentials()
    # Passkey MFA opens a browser by default; make it work on headless machines.
    install_passkey_flow(
        no_browser=True if args.no_browser else None,
        port=passkey_port,
        username=username,
    )
    return openreview.api.OpenReviewClient(
        baseurl=BASEURL,
        username=username,
        password=password,
        tokenExpiresIn=expires_in,
    )


def auth_command(args, passkey_port) -> int:
    """Log in on this machine and print a token for a machine without a browser."""
    client = password_login(args, passkey_port, expires_in=AUTH_TOKEN_LIFETIME)
    token = getattr(client, "token", None)
    if not token:
        print("Login did not produce a token. Enter a username and password.")
        return 1
    profile = getattr(client, "profile", None)
    print_token_handoff(token, getattr(profile, "id", None) or "OpenReview")
    return 0


def resolve_client(args, passkey_port):
    """Return a logged-in OpenReviewClient, or None if a token could not be used.

    Order: ``--token``, the cached token, a token pasted at the prompt (offered
    only on machines without a browser, when a terminal is attached), then
    username/password with MFA.
    """
    token, source = None, None
    if args.token:
        token, source = normalize_token(args.token), "--token"
    else:
        token = load_cached_token()
        if token:
            source = "cache"
        elif (args.no_browser or is_headless()) and sys.stdin.isatty():
            print("\nNo browser here. To skip the password + MFA login, paste a token from")
            print("`openreview-dl --auth` run on a machine with a browser.")
            pasted = getpass.getpass(
                "Token (input hidden; press Enter to log in with a password instead): "
            )
            if pasted.strip():
                token, source = normalize_token(pasted), "paste"

    if not token:
        client = password_login(args, passkey_port)
        # Reuse this session next time instead of asking for the password and MFA again.
        new_token = getattr(client, "token", None)
        if new_token:
            save_token(new_token)
        return client

    if token_is_expired(token):
        print(f"That token has expired ({describe_expiry(token)}).")
        print(NEW_TOKEN_HINT)
        return None

    client = openreview.api.OpenReviewClient(baseurl=BASEURL, token=token)
    profile, reason, definitive = token_owner(client, token)
    if profile is None:
        if not definitive:
            print(f"Could not check the token with OpenReview: {reason}.")
            if source == "cache":
                print("The cached token was kept; check the connection and try again.")
            else:
                print("Check the connection and try again.")
            return None
        print(f"OpenReview did not accept the token: {reason}.")
        if source == "cache":
            delete_token()
        print(NEW_TOKEN_HINT)
        return None
    print(f"Logged in as {profile.get('id')} with a token ({describe_expiry(token)}).")
    if source != "cache":
        save_token(token)
    return client


def download_command(args, passkey_port) -> int:
    """Prompt for a forum URL, log in, and write the Markdown file."""
    can_pick = picker.can_run_picker()
    hint = " (or press Enter to pick from your submissions)" if can_pick else ""
    url = input(f"Enter the OpenReview URL{hint}: ")
    if not url.strip() and can_pick:
        return bulk_command(args, passkey_port, "pick")
    forum_id, venue_id = parse_openreview_url(url)
    if not forum_id or not venue_id:
        print("Error: Couldn't extract forum ID or venue ID from the URL.")
        return 1

    print(f"Extracted forum ID: {forum_id}")
    print(f"Extracted venue ID: {venue_id}")

    client = resolve_client(args, passkey_port)
    if client is None:
        return 1

    venue_group = client.get_group(venue_id)
    notes = client.get_notes(forum=forum_id)
    markdown_output = generate_markdown([note.__dict__ for note in notes])
    print(markdown_output)

    # Create output directory
    output_dir = ensure_output_dir("output")

    # Save markdown file
    markdown_filename = output_dir / f"{forum_id}.md"
    markdown_filename = get_unique_filename(str(markdown_filename))
    with open(markdown_filename, "w", encoding="utf-8") as md_file:
        md_file.write(markdown_output)
    print(f"Markdown file created: {markdown_filename}")
    return 0


# ---------------------------------------------------------------------------
# Everything the user can reach: --pick, --all, --list
#
# discover.list_submissions() finds the submissions the logged-in user
# authored or holds a role on (reviewer, area chair, ...), replies included.
# They are either all downloaded, or offered in a terminal selector first.
# ---------------------------------------------------------------------------

# Pause between forums that need a request of their own; OpenReview allows a
# logged-in user only a few hundred requests per hour.
BULK_PAUSE = 0.5
# Stop a batch when this many forums in a row fail: something bigger is wrong
# (network down, session gone) and the remaining ones would fail the same way.
MAX_CONSECUTIVE_FAILURES = 3

_ROLE_ALIASES = {
    "ac": "areachair",
    "sac": "seniorareachair",
    "ae": "actioneditor",
    "pc": "reviewer",
}


def _role_key(role: str) -> str:
    key = re.sub(r"[^a-z]", "", role.lower())
    if key not in _ROLE_ALIASES and key.endswith("s"):
        key = key[:-1]
    return _ROLE_ALIASES.get(key, key)


def filter_by_role(submissions, wanted):
    """Keep submissions on which the user holds one of the ``wanted`` roles.

    Matching ignores case, punctuation and a plural "s" ("reviewers",
    "area-chair", "AC"). A submission is then listed under the matching role.
    """
    keys = {_role_key(role) for role in wanted if _role_key(role)}
    kept = []
    for sub in submissions:
        matching = [role for role in sub.roles if _role_key(role) in keys]
        if matching:
            sub.roles = matching + [role for role in sub.roles if role not in matching]
            kept.append(sub)
    return discover.sort_submissions(kept)


def _slug(text, limit=None) -> str:
    """A file-name-safe version of ``text`` (may be empty)."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-.")
    if limit:
        slug = slug[:limit].rstrip("-.")
    return slug


def submission_paths(sub, output_dir):
    """Where a submission is saved: <out>/<venue>/<number>_<title>_<forum id>.md"""
    directory = Path(output_dir) / (_slug(sub.venue_id) or "unknown-venue")
    parts = [
        str(sub.number) if sub.number is not None else "",
        _slug(sub.title, limit=60),
        _slug(sub.forum) or "forum",
    ]
    base = "_".join(part for part in parts if part)
    return directory / f"{base}.md"


def _describe(sub) -> str:
    number = f" #{sub.number}" if sub.number is not None else ""
    title = picker._clean(str(sub.title))  # written by other people: no control codes
    if len(title) > 70:
        title = title[:69] + "…"
    return f"{sub.role} · {sub.venue_id}{number} · {title}"


def print_listing(submissions, unresolved=()):
    """Print the submissions grouped by role and venue (for --list)."""
    group = None
    for sub in submissions:
        if (sub.role, sub.venue_id) != group:
            group = (sub.role, sub.venue_id)
            count = sum(1 for s in submissions if (s.role, s.venue_id) == group)
            print(f"\n{sub.role} · {sub.venue_id} ({count})")
        number = f"#{sub.number}" if sub.number is not None else ""
        title = picker._clean(str(sub.title))
        print(f"  {number:>7}  {sub.forum}  [{picker._clean(str(sub.status))}]  {title}")
    print(f"\n{len(submissions)} submission(s).")
    _print_unresolved(unresolved)


def _print_unresolved(unresolved):
    if not unresolved:
        return
    print(
        f"\n{len(unresolved)} paper(s) you hold a role on could not be read and are "
        "left out (usually deleted submissions; otherwise not visible to you yet, "
        "or a venue with an unusual setup):"
    )
    for item in unresolved:
        print(f"  {item.venue_id} #{item.number} ({', '.join(item.roles) or 'member'})")


def _error_status(error):
    args = getattr(error, "args", None) or ()
    if args and isinstance(args[0], dict):
        return args[0].get("status"), args[0].get("name")
    return None, None


def save_submission(client, sub, output_dir):
    """Write one submission's forum as Markdown; returns ``(path, note)``.

    Unlike the single-URL download this replaces the file of an earlier run
    instead of adding ``_1`` copies, and includes every readable note.
    """
    notes = sub.notes
    if notes is None:
        fetched = client.get_notes(forum=sub.forum, limit=1000)
        if len(fetched) >= 1000:  # a single page is 1000 notes at most
            fetched = client.get_all_notes(forum=sub.forum)
        notes = [note.__dict__ for note in fetched]
    if not notes:
        raise RuntimeError("nothing readable in this forum")

    text = generate_markdown(notes, include_all=True)
    md_path = submission_paths(sub, output_dir)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    partial = md_path.with_name(md_path.name + ".part")
    partial.write_text(text + "\n", encoding="utf-8")
    os.replace(partial, md_path)  # never leave a half-written file under the real name

    note = "no replies yet" if len(notes) == 1 else ""

    # A retitled paper would otherwise leave its old file next to the new one.
    # Done last, so a failed write never costs the copy of an earlier run.
    forum = glob.escape(_slug(sub.forum))
    for stale in md_path.parent.glob(f"*_{forum}.md"):
        if stale != md_path:
            try:
                stale.unlink()
            except OSError:
                pass
    return md_path, note


def download_submissions(client, submissions, output_dir) -> int:
    """Save every submission; one failure does not stop the rest. Returns an exit code."""
    total = len(submissions)
    saved, failed, consecutive = 0, [], 0
    aborted = None
    try:
        for index, sub in enumerate(submissions, 1):
            needs_request = sub.notes is None
            if needs_request and index > 1:
                time.sleep(BULK_PAUSE)
            print(f"[{index}/{total}] {_describe(sub)}")
            try:
                path, note = save_submission(client, sub, output_dir)
            except Exception as e:  # openreview, requests, OS: record it and go on
                reason = discover._reason(e)
                failed.append((sub, reason))
                consecutive += 1
                print(f"    failed: {reason}")
                status, name = _error_status(e)
                if status == 401 or name in ("TokenExpiredError", "ChallengeRequiredError"):
                    aborted = "OpenReview no longer accepts this session."
                elif consecutive >= MAX_CONSECUTIVE_FAILURES:
                    aborted = f"{consecutive} downloads in a row failed."
                if aborted:
                    break
                continue
            consecutive = 0
            saved += 1
            print(f"    {path}" + (f" ({note})" if note else ""))
    except KeyboardInterrupt:
        print(f"\nInterrupted after {saved} of {total}.")
        raise

    print(f"\nSaved {saved} of {total} submission(s) under {Path(output_dir)}{os.sep}")
    if aborted:
        print(f"Stopped early: {aborted} {total - saved - len(failed)} not attempted.")
    if failed:
        print("Failed:")
        for sub, reason in failed:
            print(f"  {_describe(sub)}: {reason}")
    return 0 if saved == total else 1


def bulk_command(args, passkey_port, mode) -> int:
    """List what the user can reach, then download all of it or a selection.

    ``mode`` is "all", "pick" (terminal selector) or "list" (print and exit).
    """
    roles = [
        role.strip()
        for value in (getattr(args, "role", None) or [])
        for role in value.split(",")
        if role.strip()
    ]
    output_dir = getattr(args, "output_dir", None) or "output"

    if mode == "pick" and not picker.can_run_picker():
        print("The selector needs an interactive terminal. Use --list to see your")
        print("submissions, or --all to download all of them.")
        return 1

    client = resolve_client(args, passkey_port)
    if client is None:
        return 1

    print("Looking up your submissions on OpenReview...")
    try:
        submissions, unresolved = discover.list_submissions(client)
    except KeyboardInterrupt:
        raise
    except Exception as e:
        # Not handle_openreview_error: it offers to delete the cached login,
        # which a failed listing does not call for.
        print(f"Could not list your submissions: {discover._reason(e)}")
        status, name = _error_status(e)
        if status == 401 or name == "TokenExpiredError":
            print(NEW_TOKEN_HINT)
        return 1
    if roles:
        available = sorted(
            {role for item in [*submissions, *unresolved] for role in item.roles}
        )
        submissions = filter_by_role(submissions, roles)
        unresolved = [
            item for item in unresolved
            if {_role_key(r) for r in item.roles} & {_role_key(r) for r in roles}
        ]
        if not submissions:
            print(f"No submissions with role {', '.join(roles)}.")
            if available:
                print(f"Your roles: {', '.join(available)}.")
            _print_unresolved(unresolved)
            return 1
    if not submissions:
        print("No submissions found for this account.")
        _print_unresolved(unresolved)
        return 1

    if mode == "list":
        print_listing(submissions, unresolved)
        return 0

    _print_unresolved(unresolved)
    if mode == "pick":
        try:
            chosen = picker.pick(submissions)
        except RuntimeError as e:
            print(f"The selector could not run: {e}.")
            print("Use --list to see your submissions, or --all to download all of them.")
            return 1
        if not chosen:
            print("Nothing selected.")
            return 0
        submissions = chosen
    return download_submissions(client, submissions, output_dir)


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Download OpenReview papers and reviews"
    )
    parser.add_argument(
        "--wipe-credentials",
        action="store_true",
        help="Delete cached credentials and the cached login token, then exit",
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help=(
            "Log in on this machine (opening a browser for a passkey if needed) "
            "and print a login token for openreview-dl on a machine without a "
            "browser, then exit"
        ),
    )
    parser.add_argument(
        "--token",
        metavar="TOKEN",
        help=(
            "Log in with a token printed by `openreview-dl --auth` on another "
            "machine; it is cached until it expires. Prefer pasting the token at "
            "the prompt that machines without a browser show: that keeps it out "
            "of your shell history."
        ),
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help=(
            "Treat this machine as having no browser: offer to paste a token from "
            "`openreview-dl --auth`, and for passkey MFA print the login URL and "
            "port-forwarding instructions instead of opening a browser (also: "
            f"{NO_BROWSER_ENV}=1). Enabled automatically in SSH sessions and, on "
            "Linux, when no display is available."
        ),
    )
    parser.add_argument(
        "--passkey-port",
        type=int,
        metavar="PORT",
        help=(
            "Local port that receives the passkey login callback (default: any "
            f"free port; also: {PASSKEY_PORT_ENV}). Use a fixed port so an SSH "
            "port forward can be set up once."
        ),
    )
    bulk = parser.add_argument_group(
        "everything you have access to",
        "Find the submissions you authored or hold a role on (reviewer, area "
        "chair, ...) instead of asking for one URL. API v2 venues only.",
    )
    which = bulk.add_mutually_exclusive_group()
    which.add_argument(
        "--pick",
        action="store_true",
        help="Choose what to download in an interactive selector",
    )
    which.add_argument(
        "--all",
        action="store_true",
        help="Download every submission you have access to",
    )
    which.add_argument(
        "--list",
        action="store_true",
        help="Print the submissions you have access to and exit",
    )
    bulk.add_argument(
        "--role",
        action="append",
        metavar="ROLE",
        help=(
            "With --pick/--all/--list: only submissions where you are ROLE, e.g. "
            "author, reviewer, area-chair (repeat or separate with commas)"
        ),
    )
    bulk.add_argument(
        "--output-dir",
        metavar="DIR",
        help=(
            "With --pick/--all: where to write the files (default: output). Each "
            "venue gets a sub-directory; files from an earlier run are replaced."
        ),
    )
    args = parser.parse_args()
    mode = "pick" if args.pick else "all" if args.all else "list" if args.list else None
    if mode and (args.auth or args.wipe_credentials):
        parser.error("--pick/--all/--list cannot be combined with --auth or --wipe-credentials")
    if not mode and (args.role is not None or args.output_dir is not None):
        parser.error("--role and --output-dir need --pick, --all or --list")
    if args.role is not None and not any(
        role.strip() for value in args.role for role in value.split(",")
    ):
        parser.error("--role needs a role, e.g. author, reviewer or area-chair")
    if args.output_dir is not None and not args.output_dir.strip():
        parser.error("--output-dir needs a directory")

    # Titles contain any character; a console or pipe that cannot encode one
    # (cp1252 on Windows, an ASCII locale) must not abort a batch.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    if args.wipe_credentials:
        delete_credentials()
        return 0

    # Resolve passkey MFA options up front so bad values fail before any prompt.
    if args.passkey_port is not None and not 0 <= args.passkey_port <= 65535:
        parser.error("--passkey-port must be between 0 and 65535")
    try:
        passkey_port = (
            args.passkey_port if args.passkey_port is not None else get_passkey_port()
        )
    except ValueError as e:
        parser.error(str(e))

    try:
        if args.auth:
            return auth_command(args, passkey_port)
        if mode:
            return bulk_command(args, passkey_port, mode)
        return download_command(args, passkey_port)
    except openreview.openreview.OpenReviewException as e:
        return handle_openreview_error(e)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as e:
        detail = str(e) or "no details"
        print(f"An unexpected error occurred ({type(e).__name__}): {detail}")
        return 1


if __name__ == "__main__":
    exit(main())
