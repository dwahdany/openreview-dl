# Developing openreview-dl

How to set up, test, document and release openreview-dl. For what the tool does and how to use
it, see the [README](README.md).

- [Setup](#setup)
- [Tests](#tests)
- [Trying the selector without an account](#trying-the-selector-without-an-account)
- [Screenshots](#screenshots)
- [Layout of the code](#layout-of-the-code)
- [How submissions are found](#how-submissions-are-found)
- [The selector](#the-selector)
- [Stored logins](#stored-logins)
- [Releasing](#releasing)

## Setup

The project is managed with [uv](https://docs.astral.sh/uv/); nothing else is needed.

```sh
git clone https://github.com/dwahdany/openreview-dl && cd openreview-dl
uv sync                      # creates .venv with the runtime and dev dependencies
uv run openreview-dl --help  # runs the working copy
```

Python 3.11 or later; `.python-version` pins 3.13 for local work. Add a dependency with
`uv add <package>` so that `pyproject.toml` and `uv.lock` change together.

## Tests

```sh
uv run pytest                 # everything, about 370 tests
uv run pytest tests/test_picker.py -q
```

The tests never contact OpenReview and never touch your real `~/.config/openreview-dl`: an
autouse fixture in `tests/conftest.py` points `Path.home()` at a temporary directory, and the
OpenReview client is replaced by small fakes.

| File | Covers |
|---|---|
| `tests/test_cli.py` | Login, tokens, the credential cache, passkey callback, URL parsing, Markdown rendering |
| `tests/test_discover.py` | Finding submissions from authorship and per-paper groups, failure handling |
| `tests/test_bulk.py` | `--pick` / `--all` / `--list`, `--role`, file naming, the download loop |
| `tests/test_picker.py` | The selector, driven headlessly through Textual's pilot |

The selector tests are plain synchronous pytest (no `pytest-asyncio`). `pick(items,
_auto_pilot=fn)` runs the app headlessly and returns its result; if the pilot runs out of keys
while the app is still open, the result is `picker.STILL_OPEN` rather than a hang. Tests that
need the terminal check replace `picker.can_run_picker`, because `pytest -s` hands the real
terminal to the test process.

CI (`.github/workflows/test.yml`) runs the suite on Linux, macOS and Windows with Python 3.11,
3.12 and 3.13. The supported Textual range is `>=6.0.0,<9`; to check the lower end:

```sh
uv run --with 'textual==6.0.0' pytest -q tests/test_picker.py
```

## Trying the selector without an account

`scripts/screenshots.py` holds a set of invented submissions. To use the selector on them in
your terminal:

```sh
uv run python -c "
import sys; sys.path.insert(0, 'scripts')
import screenshots
from openreview_dl import picker
print(picker.pick(screenshots.demo_items()))"
```

## Screenshots

The pictures in the README show invented submissions, not anybody's account.

```sh
uv run python scripts/screenshots.py     # rewrites docs/images/*.png
```

The script runs the selector headlessly, presses a fixed sequence of keys, and lets Textual
write the screen as an SVG. A headless Chrome or Chromium then renders each SVG to a PNG at
twice the size. It is found on `PATH` or in `/Applications`; set `CHROME` to point at another
one. Chrome is used rather than librsvg because the SVG relies on a web font (Fira Code, fetched
from cdnjs) and on `textLength`, and librsvg honours neither, which pushes the columns out of
line.

The README refers to the pictures by absolute `raw.githubusercontent.com` URLs on `main`, so
that they also show on PyPI, which does not resolve relative paths. A new picture therefore
appears only after it has been pushed to `main`.

## Layout of the code

| File | Contents |
|---|---|
| `src/openreview_dl/cli.py` | Argument parsing, login (password, two-factor, passkey, tokens), the credential cache, Markdown rendering, the single-URL download and the `--pick` / `--all` / `--list` commands |
| `src/openreview_dl/discover.py` | Finds every submission the logged-in account can reach |
| `src/openreview_dl/picker.py` | The terminal selector (Textual) |
| `scripts/screenshots.py` | Regenerates the README pictures |

Rendering has two modes. The single-URL download calls `generate_markdown(notes)` and writes
only the reviewers' threads. The bulk commands call `generate_markdown(notes, include_all=True)`,
which adds sections for every other signer (area chairs, program chairs, authors' top-level
comments) and an "Other notes" section for replies whose parent cannot be read, so that nothing
a reviewer or chair can see is dropped. Heading levels follow the reply depth and stop at level
6; deeper replies carry an "In reply to" line instead.

Bulk downloads write `<name>.md.part` and rename it, so an interrupted run never leaves a
half-written file under the real name, and files of an earlier run with another title are
removed only after the new ones exist. The loop stops early after three failures in a row or
when OpenReview rejects the session.

## How submissions are found

`discover.list_submissions(client)` combines the two sources the OpenReview web consoles use:

1. **Authored papers:** `get_all_notes(content={'authorids': <profile id>}, details='replies')`.
   Notes without a review forum (DBLP imports and similar archive entries) are left out.
2. **Roles:** `GET /groups?member=<profile id>` returns every group the account is in. Per-paper
   groups look like `<venue>/Submission<N>/<Role>` (some venues say `Paper<N>`); the group's
   `domain` gives the venue id. The paper numbers are then looked up, fifty at a time, with
   `get_notes(invitation=<venue>/-/Submission, number='1,2,3', details='replies')`. If that
   invitation finds nothing, `<venue>/-/<token>` and then the venue group's configured
   `submission_id` are tried.

Replies arrive together with the submissions (`details='replies'`), so downloading a listed
submission costs no further request.

Things that were measured against the live API (November 2026) and shaped the code:

- A logged-in account is allowed roughly 500 `/notes` and 700 `/groups` requests per hour. This
  is why replies are fetched with the listing rather than per forum, and why a rate limit, an
  expired login or a network error aborts the listing (`_affects_every_venue`) instead of being
  reported per venue: carrying on would produce a listing that looks complete and is not.
- `details='replies'` returns the same notes as `get_notes(forum=...)`.
- Papers that stay unresolved are, in practice, deleted submissions: the per-paper group
  outlives the note. They are returned separately as `Unresolved` and reported to the user.
- Venues on the legacy API v1 are not covered; the client only talks to `api2.openreview.net`.

## The selector

`picker.build_app(items)` returns a Textual `App`; Textual is imported inside that function, so
runs that never open the selector do not pay for the import. `picker.pick(items)` runs it and
returns the chosen items, `None` when cancelled, and `[]` only for empty input.

Notes for whoever changes it:

- It is built on `OptionList`, not `SelectionList`. Group headings are disabled options and the
  app keeps its own set of ticked indices. `SelectionList` cannot hold headings or separators,
  toggles on Enter, and forgets its selection when the options are rebuilt for a filter.
- `can_run_picker()` requires stdin, stdout and stderr to be terminals and `TERM` not to be
  `dumb`. Textual reads keys from stdin, draws on stderr (stdout on Windows) and measures the
  terminal through stdout. Calling `App.run()` without a terminal hangs, so `pick()` refuses.
- Titles are other people's text. `_clean()` removes control and zero-width characters, and rows
  are assembled with `Content.assemble`, so a title is never parsed as markup.
- A few overrides work around `OptionList` behaviour and are covered by tests: Page Up near the
  top (the base class drops the highlight when only a heading is in range), scrolling the first
  heading back into view, and keys that arrive in the same read as `/` (they are typed into the
  filter instead of acting as shortcuts).
- Textual installs `SIGTSTP`, `SIGCONT` and `SIGWINCH` handlers and leaves them in place;
  `pick()` restores the previous ones so that Ctrl-Z works during the download that follows.
- A crash inside the app makes Textual print the traceback and return `None`, which would look
  like a cancel; `pick()` checks `app.return_code` and raises instead.

Not verified: everything on Windows beyond the headless tests in CI, including how `·`, `…`
and the Enter symbol in the footer look in a legacy console.

## Stored logins

Everything lives in `~/.config/openreview-dl/`, written with mode 0600:

| File | Contents |
|---|---|
| `key` | Random Fernet key |
| `credentials.enc` | Username and password, if the user chose to cache them |
| `token.enc` | The current login token, until it expires |

Caches written before 0.4 used a key derived from the hostname, which broke whenever macOS
picked a different hostname on a new network. Such files are converted to the random key on
first use. A cache that cannot be decrypted is left alone and the user is asked to log in
again; caching again replaces it. If OpenReview cannot be reached while a cached token is
checked, the token is kept for the next attempt.

A token that arrives through the passkey callback or `--token` is checked against OpenReview
and, where a username is known, against that account before it is used or cached.

## Releasing

Releases go to PyPI through trusted publishing: `.github/workflows/publish.yml` builds with
`uv build` and uploads when a GitHub release is published. No API token is involved.

1. Set the new version in `pyproject.toml` and in `src/openreview_dl/__init__.py`, then run
   `uv lock` so that `uv.lock` records it.
2. Commit, and get it onto `main` with the tests green.
3. Tag and publish the release:

   ```sh
   git tag v0.5.0 && git push origin v0.5.0
   gh release create v0.5.0 --title "v0.5.0 - <what changed>" --notes "<release notes>"
   ```

4. When the *Publish to PyPI* workflow has finished, check the result:

   ```sh
   uvx openreview-dl@latest --help
   ```

Versions follow [Semantic Versioning](https://semver.org/).

The one-time PyPI setup, should it ever be needed again: under *Publishing* in the PyPI account
settings, add a trusted publisher with owner `dwahdany`, repository `openreview-dl`, workflow
`publish.yml` and no environment.
