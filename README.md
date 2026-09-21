# openreview-dl

Download OpenReview paper reviews and rebuttals as formatted documents (ODT and Markdown).

## Features

- Extracts forum ID and venue ID from an OpenReview URL
- Optional credential caching (machine-specific encryption)
- Fetches paper details, reviews, and rebuttals
- Generates a formatted markdown document
- Converts the markdown to an ODT file for easy reading
- Organizes reviews by reviewer with threaded replies

## Installation

### Using uvx (recommended - no installation needed)

```bash
uvx openreview-dl
```

### Using uv

```bash
uv tool install openreview-dl
```

### Using pip

```bash
pip install openreview-dl
```

## Usage

Run the command and follow the prompts:

```bash
openreview-dl
```

Or with uvx:

```bash
uvx openreview-dl
```

You'll be prompted to:
1. Enter the full OpenReview URL (example: `https://openreview.net/forum?id=XXXXXXXXXXXX&referrer=%5BAuthor%20Console%5D(%2Fgroup%3Fid%3DConference.org%2FYYYY%2FMeeting%2FAuthors%23your-submissions)`)
2. Provide your OpenReview username and password
3. Optionally cache credentials for future use (stored in `~/.config/openreview-dl/credentials.enc` with machine-specific encryption)

The tool will generate:
- `output/$FORUM_ID.md` - Markdown formatted file
- `output/$FORUM_ID.odt` - ODT document (can be opened in LibreOffice, Microsoft Word, etc.)

where `$FORUM_ID` is the paper ID extracted from the URL.

## Multi-factor authentication

If your OpenReview account has MFA enabled you will be asked to pick a method after entering your password. `emailOtp` and `totp` work anywhere: type the code when prompted. `passkey` needs a browser, which the tool opens for you on a desktop.

### Machines without a browser (SSH sessions, servers, containers)

Log in once on a machine that has a browser and hand the token over:

1. On your laptop run `openreview-dl --auth`. It logs in (the browser opens for the passkey) and prints a login token that stays valid for up to a week.
2. On the headless machine run `openreview-dl` as usual. Because no browser is available it offers to take a token before falling back to a password login. Paste the token at that prompt (input is hidden). `--token TOKEN` also works but leaves the token in your shell history.

The token is checked against OpenReview, cached in `~/.config/openreview-dl/token.enc` (encrypted like the credentials, file readable only by your user) and reused until it expires. When it has expired, repeat step 1. If OpenReview cannot be reached during the check, the cached token is kept for the next attempt. `--wipe-credentials` removes it.

### Passkey over an SSH port forward

To finish a passkey login from the headless machine itself instead, choose `passkey` at the MFA prompt. The tool prints the login URL and this hint:

```
That page posts the token to 127.0.0.1:54321 on the browser's machine, so first
forward the port to this machine (use your usual user@host or ssh alias):

  ssh -L 54321:127.0.0.1:54321 you@remote-host
```

Run that from your laptop, open the URL in your local browser, and finish the passkey prompt. `--passkey-port PORT` (or `OPENREVIEW_DL_PASSKEY_PORT=PORT`) fixes the port so a `LocalForward` line in your `~/.ssh/config` covers every login. `--no-browser` (or `OPENREVIEW_DL_NO_BROWSER=1`) treats the machine as browserless (token prompt plus this passkey behaviour) when the automatic detection (SSH session, or Linux without a display) misses. Tokens arriving this way are verified to belong to your account before they are used.

## Credential Caching

If you choose to cache credentials, they are stored at:
- **Linux/macOS**: `~/.config/openreview-dl/credentials.enc`

The credentials are encrypted using machine-specific keys (hostname-based), so they won't work if copied to another machine. However, this is not fully secure - anyone with access to your user account can potentially decrypt them.

## Note

Ensure you have the necessary permissions to access the paper on OpenReview. You must be logged in with an account that has access to the reviews (typically as an author, reviewer, or area chair).