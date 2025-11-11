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

## Credential Caching

If you choose to cache credentials, they are stored at:
- **Linux/macOS**: `~/.config/openreview-dl/credentials.enc`

The credentials are encrypted using machine-specific keys (hostname-based), so they won't work if copied to another machine. However, this is not fully secure - anyone with access to your user account can potentially decrypt them.

## Note

Ensure you have the necessary permissions to access the paper on OpenReview. You must be logged in with an account that has access to the reviews (typically as an author, reviewer, or area chair).