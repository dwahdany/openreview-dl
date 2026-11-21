"""Interactive multi-select of OpenReview submissions, built on Textual.

Rows need the attributes ``role``, ``venue``, ``number``, ``title`` and
``status`` (see :class:`openreview_dl.discover.Submission`).
"""

from __future__ import annotations

import os
import signal
import sys
import unicodedata
from typing import Sequence

STILL_OPEN = "still-open"  # test-only result: the pilot finished, the UI did not


def can_run_picker() -> bool:
    """True when a full-screen terminal UI can run here.

    Textual reads keys from stdin, draws on stderr (stdout on Windows) and
    measures the terminal through stdout, so all three must be a terminal.
    """
    if sys.platform != "win32" and os.environ.get("TERM", "") in ("", "dumb"):
        return False
    streams = (sys.__stdin__, sys.__stdout__, sys.__stderr__)
    try:
        return all(stream is not None and stream.isatty() for stream in streams)
    except ValueError:  # closed file
        return False


def _clean(text: str) -> str:
    """One line of printable text: titles come from other people."""
    kept = (
        c if c.isprintable() else " "
        for c in text
        if unicodedata.category(c) != "Cf"  # soft hyphen, ZWJ, bidi overrides
    )
    return " ".join("".join(kept).split())


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def _matches(item, tokens: Sequence[str]) -> bool:
    haystack = " ".join(
        (
            item.role,
            item.venue,
            f"#{item.number}" if item.number is not None else "",
            item.title,
            item.status,
        )
    ).casefold()
    return all(token in haystack for token in tokens)


def build_app(items, *, title: str = "Select submissions to download"):
    """Return the picker App. Textual is imported here, not at module import."""
    from textual import events
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.content import Content
    from textual.widgets import Footer, Input, OptionList, Static
    from textual.widgets.option_list import Option

    items = list(items)
    number_width = max(
        (len(str(i.number)) for i in items if i.number is not None), default=1
    )
    status_width = min(max((len(i.status) for i in items), default=0), 28)

    class Rows(OptionList):
        # OptionList binds enter to "select" (posts OptionSelected). Rebinding
        # the same keys here wins over the base class, so Enter confirms and
        # Space toggles; a mouse click still posts OptionSelected (= toggle).
        BINDINGS = [
            Binding("space", "app.toggle", "Toggle"),
            Binding("enter", "app.confirm", "Download selected"),
            Binding("j", "cursor_down", show=False),
            Binding("k", "cursor_up", show=False),
        ]

        def action_page_up(self) -> None:
            # Option 0 is a disabled heading: within a page of the top the
            # base class finds nothing enabled and drops the highlight.
            super().action_page_up()
            if self.highlighted is None:
                self.action_first()

        def watch_highlighted(self, highlighted: int | None) -> None:
            super().watch_highlighted(highlighted)
            # Headings are disabled options, so the cursor never lands on one
            # and OptionList alone would leave it scrolled out above its rows.
            try:
                if highlighted and self.get_option_at_index(highlighted - 1).disabled:
                    line = self._index_to_line.get(highlighted - 1)
                    if line is not None and line < self.scroll_y:
                        self.scroll_to(y=line, animate=False, immediate=True)
            except Exception:  # relies on OptionList internals: never fatal
                pass

    class Filter(Input):
        BINDINGS = [Binding("down", "app.focus_list", show=False)]

    class Picker(App):
        CSS = """
        #title { padding: 0 1; text-style: bold; }
        #filter { border: none; height: 1; padding: 0 1; }
        #rows { height: 1fr; border: none; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
        #rows > .option-list--option-disabled { color: $text-accent; text-style: bold; }
        #count { padding: 0 1; color: $text-muted; }
        """
        ENABLE_COMMAND_PALETTE = False
        BINDINGS = [
            Binding("a", "select_all", "All"),
            Binding("n", "select_none", "None"),
            Binding("slash", "focus_filter", "Filter", key_display="/"),
            Binding("escape", "back", "Cancel"),
            Binding("q", "cancel", "Quit", show=False),
            Binding("ctrl+c", "cancel", show=False, priority=True),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.chosen: set[int] = set()  # indices into `items`
            self.shown: list[int] = list(range(len(items)))

        # -- layout -----------------------------------------------------
        def compose(self) -> ComposeResult:
            yield Static(title, id="title", markup=False)
            yield Filter(placeholder="Filter: press / and type", id="filter")
            yield Rows(id="rows")
            yield Static(id="count", markup=False)
            yield Footer()

        def on_mount(self) -> None:
            self.rebuild()
            self.query_one(Rows).focus()

        # -- rendering --------------------------------------------------
        def row_prompt(self, index: int) -> Content:
            item = items[index]
            number = "" if item.number is None else f"#{item.number}"
            return Content.assemble(
                ("[x] " if index in self.chosen else "[ ] ", "bold"),
                (number.rjust(number_width + 1), "dim"),
                "  ",
                (_shorten(_clean(item.status), status_width).ljust(status_width), "italic"),
                "  ",
                _clean(item.title),  # plain str part: never parsed as markup
            )

        def rebuild(self) -> None:
            """Refill the list from `items` and the filter text."""
            rows = self.query_one(Rows)
            keep = self.highlighted_index()
            tokens = self.query_one(Filter).value.casefold().split()
            self.shown = [i for i, it in enumerate(items) if _matches(it, tokens)]

            groups: dict[tuple[str, str], list[int]] = {}
            for i in self.shown:
                groups.setdefault((items[i].role, items[i].venue), []).append(i)

            options: list[Option | None] = []
            for (role, venue), members in groups.items():
                if options:
                    options.append(None)  # separator rule under previous group
                options.append(
                    Option(
                        Content.assemble(
                            _clean(f"{role}  ·  {venue}  ({len(members)})")
                        ),
                        disabled=True,
                    )
                )
                options.extend(Option(self.row_prompt(i), id=f"row-{i}") for i in members)

            rows.clear_options()
            rows.add_options(options)
            if keep in self.shown:
                rows.highlighted = rows.get_option_index(f"row-{keep}")
            else:
                rows.action_first()  # first enabled option, skips the heading
            self.update_count()

        def update_count(self) -> None:
            hidden = len(self.chosen - set(self.shown))
            text = f"{len(self.chosen)} of {len(items)} selected"
            if len(self.shown) != len(items):
                text += f"  ·  {len(self.shown)} shown"
            if hidden:
                text += f"  ·  {hidden} selected but hidden by the filter"
            self.query_one("#count", Static).update(text)

        def highlighted_index(self) -> int | None:
            option = self.query_one(Rows).highlighted_option
            if option is None or option.id is None:
                return None
            return int(option.id.removeprefix("row-"))

        def set_chosen(self, indices, chosen: bool) -> None:
            rows = self.query_one(Rows)
            for i in indices:
                if (i in self.chosen) == chosen:
                    continue
                (self.chosen.add if chosen else self.chosen.discard)(i)
                rows.replace_option_prompt(f"row-{i}", self.row_prompt(i))
            self.update_count()

        # -- events -----------------------------------------------------
        def on_input_changed(self, event: Input.Changed) -> None:
            self.rebuild()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            self.action_focus_list()

        def on_key(self, event: events.Key) -> None:
            """Keys that arrive in the same read as "/" were already sent to the list.

            They bubble up to here once the filter is focused, where the
            a / n / q bindings would act on them. Type them instead.
            """
            field = self.query_one(Filter)
            if self.focused is field and event.is_printable and event.character:
                field.insert_text_at_cursor(event.character)
                event.prevent_default()  # skip the app-level key bindings

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            self.action_toggle()  # mouse click on a row

        # -- actions ----------------------------------------------------
        def action_toggle(self) -> None:
            index = self.highlighted_index()
            if index is not None:
                self.set_chosen([index], index not in self.chosen)

        def action_select_all(self) -> None:
            self.set_chosen(self.shown, True)

        def action_select_none(self) -> None:
            self.set_chosen(self.shown, False)

        def action_focus_filter(self) -> None:
            # Not .focus(): that is deferred, and keys that arrive in the same
            # read as "/" would still be handled as list keys (see on_key).
            self.set_focus(self.query_one(Filter))

        def action_focus_list(self) -> None:
            self.query_one(Rows).focus()

        def action_back(self) -> None:
            """Esc: leave the filter first, cancel from the list."""
            field = self.query_one(Filter)
            if field.has_focus:
                field.value = ""
                self.action_focus_list()
            else:
                self.action_cancel()

        def action_cancel(self) -> None:
            self.exit(None)

        def action_confirm(self) -> None:
            if not self.chosen:
                self.notify("Nothing selected. Space selects a row, a selects all.")
                return
            self.exit([items[i] for i in sorted(self.chosen)])

    return Picker()


def pick(
    items,
    *,
    title: str = "Select submissions to download",
    _auto_pilot=None,
):
    """Let the user tick submissions. Returns the chosen items, or None if cancelled.

    `_auto_pilot` is for tests: an `async def (pilot)` that drives the app
    headlessly, so no terminal is needed.
    """
    items = list(items)
    if not items:
        return []
    app = build_app(items, title=title)
    if _auto_pilot is not None:

        async def bounded(pilot):
            await _auto_pilot(pilot)
            await pilot.pause()
            if app.return_code is None:
                app.exit(STILL_OPEN)  # pilot ran out of keys: do not wait forever

        result = app.run(headless=True, size=(100, 30), auto_pilot=bounded)
    elif not can_run_picker():
        raise RuntimeError("the interactive selector needs a terminal")
    else:
        # Textual's driver installs SIGTSTP/SIGCONT handlers and never removes
        # them; left in place they break Ctrl-Z + fg in the download that follows.
        saved = {}
        for name in ("SIGTSTP", "SIGCONT", "SIGWINCH"):
            signum = getattr(signal, name, None)  # absent on Windows
            if signum is not None:
                saved[signum] = signal.getsignal(signum)
        try:
            result = app.run()
        finally:
            for signum, handler in saved.items():
                if handler is not None:  # None: not installed from Python
                    signal.signal(signum, handler)
    if app.return_code:  # Textual printed the traceback and returned None
        raise RuntimeError("the selector crashed (see the traceback above)")
    return result
