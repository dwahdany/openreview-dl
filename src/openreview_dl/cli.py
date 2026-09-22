import argparse
import base64
import functools
import getpass
import http.server
import importlib
import json
import os
import platform
import re
import socket
import sys
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from operator import itemgetter
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import markdown
import openreview
import pypandoc
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_pandoc_checked = False


def _ensure_pandoc():
    """Ensure pandoc is available, downloading if necessary."""
    global _pandoc_checked
    if _pandoc_checked:
        return
    try:
        pypandoc.get_pandoc_version()
    except OSError:
        print("Pandoc not found. Downloading...")
        pypandoc.download_pandoc()
    _pandoc_checked = True


def get_config_dir() -> Path:
    """Get the config directory path, creating it if it doesn't exist."""
    config_dir = Path.home() / ".config" / "openreview-dl"
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return config_dir


def write_private(path: Path, text: str):
    """Write ``text`` to ``path`` so that only the current user can read it."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)  # also tighten a file that already existed
    except OSError:
        pass


def get_credentials_path() -> Path:
    """Get the path to the credentials file."""
    return get_config_dir() / "credentials.enc"


def get_key():
    # Use a fixed salt (not ideal, but better than nothing)
    salt = b"fixed_salt_for_openreview"
    # Use the machine's hostname as a basis for the key
    hostname = platform.uname().node.encode()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(hostname))
    return key


def encrypt(text):
    f = Fernet(get_key())
    return f.encrypt(text.encode()).decode()


def decrypt(text):
    f = Fernet(get_key())
    return f.decrypt(text.encode()).decode()


def delete_credentials():
    creds_path = get_credentials_path()
    if creds_path.exists():
        creds_path.unlink()
        print(f"Cached credentials deleted from: {creds_path}")
    else:
        print("No cached credentials found.")
    delete_token()


def load_cached_credentials():
    creds_path = get_credentials_path()
    if creds_path.exists():
        with open(creds_path, "r") as f:
            creds = json.loads(decrypt(f.read()))
        return creds["username"], creds["password"]
    return None, None


def save_credentials(username, password):
    creds = {"username": username, "password": password}
    encrypted = encrypt(json.dumps(creds))
    creds_path = get_credentials_path()
    write_private(creds_path, encrypted)
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
    write_private(path, encrypt(token))
    print(f"Token cached at: {path} ({describe_expiry(token)})")


def load_cached_token():
    """The cached token if present and not expired, else None (expired ones are removed)."""
    path = get_token_path()
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            token = decrypt(f.read())
    except Exception:
        print(f"Could not read the cached token at {path}; removing it.")
        path.unlink(missing_ok=True)
        return None
    if token_is_expired(token):
        print(f"The cached token expired ({describe_expiry(token)}).")
        path.unlink(missing_ok=True)
        return None
    return token


def delete_token():
    path = get_token_path()
    if path.exists():
        path.unlink()
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


def generate_markdown(notes):
    # Separate the full paper and other notes
    full_paper = next((note for note in notes if note["replyto"] is None), None)
    other_notes = [note for note in notes if note["replyto"] is not None]

    markdown_text = ""

    # Process full paper
    if full_paper:
        markdown_text += "# Full Paper\n\n"
        markdown_text += process_full_paper(full_paper)

    # Create a dictionary to store notes by their ID
    notes_by_id = {note["id"]: note for note in other_notes}

    # Group top-level reviews by reviewer
    reviewer_groups = {}
    for note in other_notes:
        if note["replyto"] == full_paper["id"]:
            reviewer_id = extract_reviewer_id(note["signatures"])
            if reviewer_id:
                reviewer_groups[reviewer_id] = note["id"]

    # Process reviewer notes and their replies
    for reviewer_id, top_review_id in reviewer_groups.items():
        markdown_text += f"## {reviewer_id}\n\n"
        markdown_text += process_note_thread(top_review_id, notes_by_id)

    return markdown_text.strip()


def process_note_thread(note_id, notes_by_id, depth=0):
    markdown_text = ""
    note = notes_by_id[note_id]

    # Process the current note
    markdown_text += "  " * depth
    markdown_text += process_note(note, is_rebuttal="Authors" in note["signatures"][0])

    # Process replies to this note
    replies = [n for n in notes_by_id.values() if n["replyto"] == note_id]
    replies.sort(key=itemgetter("cdate"))

    for reply in replies:
        markdown_text += process_note_thread(reply["id"], notes_by_id, depth + 1)

    return markdown_text


def process_full_paper(paper):
    markdown_text = f"### Paper ID: {paper['id']}\n\n"

    content = paper["content"]
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

    try:
        # Process essential fields
        for field in essential_fields:
            if field in content:
                if field == "authors":
                    markdown_text += f"**{field.capitalize()}:** {', '.join(content[field]['value'])}\n\n"
                else:
                    markdown_text += (
                        f"**{field.capitalize()}:** {content[field]['value']}\n\n"
                    )
            else:
                print(f"Warning: Essential field '{field}' is missing.")

        # Process additional fields
        for field in additional_fields:
            if field in content:
                if isinstance(content[field]["value"], list):
                    markdown_text += f"**{field.capitalize()}:** {', '.join(content[field]['value'])}\n\n"
                else:
                    markdown_text += (
                        f"**{field.capitalize()}:** {content[field]['value']}\n\n"
                    )

    except KeyError as e:
        print(f"KeyError: {e}")
        print("Available keys in content:")
        for key in content.keys():
            print(f"- {key}")

        # Add available information to markdown
        for key, value in content.items():
            if isinstance(value, dict) and "value" in value:
                markdown_text += f"**{key.capitalize()}:** {value['value']}\n\n"

    markdown_text += "---\n\n"
    return markdown_text


def process_note(note, is_rebuttal=False):
    markdown_text = f"### {'Rebuttal to ' if is_rebuttal else ''}Note {note['number']} (ID: {note['id']})\n\n"

    content = note["content"]
    for key, value in content.items():
        if isinstance(value, dict) and "value" in value:
            markdown_text += f"**{key.capitalize()}:**\n {value['value']}\n\n"

    markdown_text += "---\n\n"
    return markdown_text


def markdown_to_odt(markdown_text, output_filename):
    _ensure_pandoc()
    # Convert markdown to HTML
    html = markdown.markdown(markdown_text)

    # Convert HTML to ODT
    pypandoc.convert_text(html, "odt", format="html", outputfile=output_filename)
    print(f"ODT file created: {output_filename}")


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
        return password_login(args, passkey_port)

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
    """Prompt for a forum URL, log in, and write the Markdown and ODT files."""
    url = input("Enter the OpenReview URL: ")
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

    # Save ODT file
    odt_filename = output_dir / f"{forum_id}.odt"
    odt_filename = get_unique_filename(str(odt_filename))
    markdown_to_odt(markdown_output, str(odt_filename))
    return 0


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
    args = parser.parse_args()

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
        return download_command(args, passkey_port)
    except openreview.openreview.OpenReviewException as e:
        return handle_openreview_error(e)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return 1


if __name__ == "__main__":
    exit(main())
