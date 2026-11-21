"""Headless tests for the picker. Plain sync pytest, no pytest-asyncio."""

import asyncio

import pytest

from openreview_dl import picker
from dataclasses import dataclass

from openreview_dl.picker import STILL_OPEN, build_app, can_run_picker, pick


@dataclass(frozen=True)
class Submission:
    role: str
    venue: str
    number: int | None
    title: str
    status: str
    forum: str = ""


DEMO = [
    Submission("Author", "ICLR 2025 Conference", 1234, "Attention is [not] all you need [/b]", "Under review", "f1"),
    Submission("Author", "ICLR 2025 Conference", 88, "A second paper", "Withdrawn", "f2"),
    Submission("Reviewer", "ICLR 2025 Conference", 4021, "Something reviewed " + "very long title " * 12, "Under review", "f3"),
    Submission("Reviewer", "ICLR 2025 Conference", 4100, "Diffusion models for $x^2$ and [red]colour[/red]", "Under review", "f4"),
    Submission("Reviewer", "NeurIPS 2024 Conference", 17, "Graph networks", "Accepted (poster)", "f5"),
    Submission("Area Chair", "ICML 2025 Conference", 9, "Bandits again", "Rejected", "f6"),
]


def drive(*keys, items=DEMO):
    """Run pick() headlessly, press keys, return what pick() returned."""

    async def auto_pilot(pilot):
        await pilot.pause()
        await pilot.press(*keys)
        await pilot.pause()

    return pick(items, _auto_pilot=auto_pilot)


def forums(result):
    return None if result is None else [s.forum for s in result]


def test_space_toggles_and_enter_confirms():
    # Cursor starts on the first row (the group heading is skipped).
    assert forums(drive("space", "down", "down", "space", "enter")) == ["f1", "f3"]


def test_space_twice_deselects():
    assert forums(drive("space", "space", "down", "space", "enter")) == ["f2"]


def test_select_all_and_none():
    assert forums(drive("a", "enter")) == ["f1", "f2", "f3", "f4", "f5", "f6"]
    assert forums(drive("a", "n", "down", "space", "enter")) == ["f2"]


@pytest.mark.parametrize("key", ["escape", "q", "ctrl+c"])
def test_cancel_returns_none(key):
    assert drive("a", key) is None


def test_filter_keeps_earlier_selection_and_all_is_scoped_to_filter():
    keys = ["space", "slash", *"neurips", "enter", "a", "enter"]
    assert forums(drive(*keys)) == ["f1", "f5"]


def test_filter_matches_role_and_venue_tokens():
    keys = ["slash", *"reviewer", "space", *"iclr", "enter", "a", "enter"]
    assert forums(drive(*keys)) == ["f3", "f4"]


def test_letters_typed_in_filter_do_not_trigger_bindings():
    # "q", "a", "n" and "/" go into the filter text while it has focus.
    keys = ["slash", *"again", "enter", "space", "enter"]
    assert forums(drive(*keys)) == ["f6"]


def test_escape_in_filter_clears_it_instead_of_cancelling():
    keys = ["slash", *"zzz", "escape", "a", "enter"]
    assert len(drive(*keys)) == 6


def test_empty_items_returns_empty_list_without_ui():
    assert pick([]) == []


def test_not_a_terminal(monkeypatch):
    # Do not rely on pytest's capture: `pytest -s` in a terminal leaves a real TTY.
    monkeypatch.setattr(picker, "can_run_picker", lambda: False)
    with pytest.raises(RuntimeError):
        pick(DEMO)


class _Stream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.mark.parametrize(
    "stdin_tty, out_tty, term, expected",
    [(True, True, "xterm-256color", True), (False, True, "xterm", False),
     (True, False, "xterm", False), (True, True, "dumb", False)],
)
def test_can_run_picker(monkeypatch, stdin_tty, out_tty, term, expected):
    monkeypatch.setattr(picker.sys, "__stdin__", _Stream(stdin_tty))
    monkeypatch.setattr(picker.sys, "__stderr__", _Stream(out_tty))
    monkeypatch.setattr(picker.sys, "__stdout__", _Stream(out_tty))
    monkeypatch.setenv("TERM", term)
    if picker.sys.platform == "win32" and term == "dumb":
        expected = True  # TERM is not consulted on Windows
    assert can_run_picker() is expected


def test_pilot_running_out_of_keys_does_not_hang():
    assert drive("down") == STILL_OPEN
    assert drive("enter") == STILL_OPEN  # nothing selected: stays open


def test_soft_hyphen_and_zero_width_chars_are_dropped():
    assert picker._clean("infor\u00admation\u200d  x\u202e\ty") == "information x y"


def test_enter_with_nothing_selected_stays_open_and_state_is_inspectable():
    """The same thing with App.run_test() and asyncio.run()."""

    async def scenario():
        app = build_app(DEMO)
        async with app.run_test(size=(60, 20)) as pilot:
            rows = app.query_one("#rows")
            await pilot.press("enter")
            await pilot.pause()
            assert app.is_running  # nothing selected: no exit
            # Markup in titles is shown literally.
            texts = [str(o.prompt) for o in rows.options]
            assert any("Attention is [not] all you need [/b]" in t for t in texts)
            assert any("[red]colour[/red]" in t for t in texts)
            # Headings are disabled options, so the cursor never lands on one.
            assert [o.disabled for o in rows.options].count(True) == 4
            for _ in range(10):
                await pilot.press("down")
                assert not rows.highlighted_option.disabled
            # A long title stays on one line at 60 columns.
            assert rows.virtual_size.height == rows.option_count + 3  # + separators
            await pilot.press("space", "enter")
            await pilot.pause()
        return app.return_value

    result = asyncio.run(scenario())
    assert len(result) == 1


def test_two_hundred_rows():
    VENUES = ["Alpha 2025", "Beta 2025", "Gamma 2025", "Delta 2025"]
    many = [
        Submission("Reviewer", VENUES[i % 4], i, f"Paper {i}", "Under review", f"f{i}")
        for i in range(200)
    ]
    keys = ["slash", *"delta", "enter", "a", "enter"]
    assert len(drive(*keys, items=many)) == 50


def test_control_characters_and_newlines_in_titles_are_flattened():
    nasty = [Submission("Author", "V", 1, "two\nlines \x1b[31mred\x07", "Under review", "f1")]

    async def scenario():
        app = build_app(nasty)
        async with app.run_test() as pilot:
            rows = app.query_one("#rows")
            text = str(rows.options[1].prompt)
            assert "two lines [31mred" in text and "\x1b" not in text and "\n" not in text
            assert rows.virtual_size.height == 2  # heading + one row

    asyncio.run(scenario())


def _many(count=36):
    venues = ["Alpha 2026", "Beta 2026", "TMLR", "Delta 2026"]
    return [
        Submission("Reviewer", venues[i % 4], i, f"Paper {i}", "Under review", f"f{i}")
        for i in range(count)
    ]


def test_page_up_at_the_top_keeps_the_cursor():
    assert forums(drive("pageup", "space", "enter", items=_many())) == ["f0"]


def test_first_heading_scrolls_back_into_view():
    async def scenario():
        app = build_app(_many(16))
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.press("end", "home")
            await pilot.pause()
            return app.query_one("#rows").scroll_y

    assert asyncio.run(scenario()) == 0


def test_keys_arriving_together_with_slash_are_typed_into_the_filter():
    from textual import events

    async def auto_pilot(pilot):
        await pilot.pause()
        for char in "/tmlr":  # no pause between keys: one terminal read
            pilot.app.post_message(events.Key("slash" if char == "/" else char, char))
        await pilot.pause()
        await pilot.pause()
        assert pilot.app.query_one("#filter").value == "tmlr"
        assert not pilot.app.chosen
        await pilot.press("enter", "a", "enter")

    assert len(pick(_many(), _auto_pilot=auto_pilot)) == 9


def test_a_crash_in_the_selector_is_an_error_not_a_cancel(capsys):
    async def auto_pilot(pilot):
        await pilot.pause()

        def boom():
            raise ValueError("broken")

        pilot.app.action_toggle = boom
        await pilot.press("space")

    with pytest.raises(RuntimeError, match="crashed"):
        pick(DEMO, _auto_pilot=auto_pilot)


def test_long_status_is_cut_with_an_ellipsis():
    assert picker._shorten("x" * 40, 28) == "x" * 27 + "…"
    assert picker._shorten("short", 28) == "short"


def test_rows_from_the_real_listing_work():
    from openreview_dl.discover import Submission as Listed

    items = [
        Listed(forum="f1", number=1, title="Mine", venue_id="Conf.cc/2026/Conference",
               status="Under review", roles=["Author", "Area Chair"]),
        Listed(forum="f2", number=None, title="Reviewed", venue_id="Conf.cc/2026/Conference",
               status="ICML MemFM 2026 Workshop Poster and more", roles=["Reviewer"]),
    ]
    assert [s.forum for s in drive("slash", *"reviewer", "enter", "a", "enter", items=items)] == ["f2"]
