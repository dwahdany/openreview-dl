"""Tests for --pick / --all / --list and the include-everything Markdown rendering."""

import sys
from types import SimpleNamespace

import pytest

from openreview_dl import cli, discover
from openreview_dl.discover import Submission, Unresolved

V = "Conf.cc/2026/Conference"


def _forum(forum="p"):
    """A forum as a reviewer sees it: two reviews, a rebuttal, a meta-review, a decision."""
    sub = f"{V}/Submission1"
    return [
        {"id": forum, "forum": forum, "replyto": None, "number": 1, "cdate": 1,
         "signatures": [f"{sub}/Authors"], "invitations": [f"{V}/-/Submission"],
         "content": {"title": {"value": "T"}, "abstract": {"value": "A"}}},
        {"id": "dec", "forum": forum, "replyto": forum, "number": 1, "cdate": 9,
         "signatures": [f"{V}/Program_Chairs"], "invitations": [f"{sub}/-/Decision"],
         "content": {"decision": {"value": "Accept"}}},
        {"id": "meta", "forum": forum, "replyto": forum, "number": 1, "cdate": 8,
         "signatures": [f"{sub}/Area_Chair_zzzz"], "invitations": [f"{sub}/-/Meta_Review"],
         "content": {"metareview": {"value": "Fine."}}},
        {"id": "r2", "forum": forum, "replyto": forum, "number": 2, "cdate": 3,
         "signatures": [f"{sub}/Reviewer_bbbb"], "invitations": [f"{sub}/-/Official_Review"],
         "content": {"summary": {"value": "Second."}}},
        {"id": "reb", "forum": forum, "replyto": "r1", "number": 3, "cdate": 4,
         "signatures": [f"{sub}/Authors"], "invitations": [f"{sub}/-/Official_Comment"],
         "content": {"comment": {"value": "Thanks."}}},
        {"id": "r1", "forum": forum, "replyto": forum, "number": 1, "cdate": 2,
         "signatures": [f"{sub}/Reviewer_aaaa"], "invitations": [f"{sub}/-/Official_Review"],
         "content": {"summary": {"value": "First."}}},
        {"id": "late", "forum": forum, "replyto": forum, "number": 5, "cdate": 7,
         "signatures": [f"{sub}/Reviewer_aaaa"], "invitations": [f"{sub}/-/Official_Comment"],
         "content": {"comment": {"value": "One more thing."}}},
    ]


def _headings(md):
    return [line for line in md.splitlines() if line.startswith("#")]


class TestGenerateMarkdownIncludeAll:
    def test_every_note_is_rendered_exactly_once(self):
        md = cli.generate_markdown(_forum(), include_all=True)
        for note_id in ("dec", "meta", "r1", "r2", "reb", "late"):
            assert md.count(f"ID: {note_id})") == 1

    def test_sections_and_order_are_stable(self):
        md = cli.generate_markdown(_forum(), include_all=True)
        assert _headings(md) == [
            "# Full Paper",
            "### Paper ID: p",
            "## Reviewer aaaa",
            "### Official Review by Reviewer aaaa (Note 1, ID: r1)",
            "#### Official Comment by Authors (Note 3, ID: reb)",
            "### Official Comment by Reviewer aaaa (Note 5, ID: late)",
            "## Reviewer bbbb",
            "### Official Review by Reviewer bbbb (Note 2, ID: r2)",
            "## Area Chair zzzz",
            "### Meta Review by Area Chair zzzz (Note 1, ID: meta)",
            "## Program Chairs",
            "### Decision by Program Chairs (Note 1, ID: dec)",
        ]
        assert cli.generate_markdown(list(reversed(_forum())), include_all=True) == md

    def test_anonymous_paper_does_not_warn_about_missing_authors(self, capsys):
        cli.generate_markdown(_forum(), include_all=True)
        assert "Warning" not in capsys.readouterr().out

    def test_reply_to_an_unreadable_note_lands_under_other_notes(self):
        notes = _forum() + [
            {"id": "orphan", "forum": "p", "replyto": "hidden", "number": 6, "cdate": 6,
             "signatures": [f"{V}/Submission1/Authors"], "content": {"comment": {"value": "x"}}},
        ]
        md = cli.generate_markdown(notes, include_all=True)
        assert _headings(md)[-2:] == ["## Other notes", "### Note by Authors (Note 6, ID: orphan)"]

    def test_forum_without_a_readable_submission(self):
        md = cli.generate_markdown(_forum()[1:], include_all=True)
        assert "# Full Paper" not in md
        assert "## Reviewer aaaa" in md and "## Program Chairs" in md

    def test_odd_notes_do_not_raise(self):
        notes = [
            {"id": "p", "replyto": None, "content": None},
            {"id": "a", "replyto": "p", "signatures": [], "content": None, "cdate": None},
            {"id": "b", "replyto": "p", "cdate": 3},
            "not a note",
        ]
        md = cli.generate_markdown(notes, include_all=True)
        assert md.count("ID: a)") == 1 and md.count("ID: b)") == 1

    def test_deep_threads_stop_at_heading_level_six(self):
        notes = [{"id": "p", "replyto": None, "content": {}}]
        parent = "p"
        for i in range(6):
            notes.append({"id": f"n{i}", "replyto": parent, "number": i, "cdate": i,
                          "signatures": [f"{V}/Submission1/Reviewer_aaaa"], "content": {}})
            parent = f"n{i}"
        md = cli.generate_markdown(notes, include_all=True)
        assert max(len(h.split(" ")[0]) for h in _headings(md)) == 6
        assert not any(line.startswith(" ") for line in md.splitlines())


class TestGenerateMarkdownDefault:
    def test_only_reviewer_threads_and_the_old_headings(self):
        md = cli.generate_markdown(_forum())
        assert _headings(md) == [
            "# Full Paper",
            "### Paper ID: p",
            "## bbbb",
            "### Note 2 (ID: r2)",
            "## aaaa",
            "### Note 1 (ID: r1)",
            "#### Rebuttal to Note 3 (ID: reb)",
            "### Note 5 (ID: late)",
        ]

    def test_empty_forum(self):
        assert cli.generate_markdown([]) == ""


def _sub(forum="f1", number=1, title="A title", role="Author", venue=V, notes="default"):
    return Submission(
        forum=forum, number=number, title=title, venue_id=venue, status="Under review",
        roles=[role], cdate=number, notes=_forum(forum) if notes == "default" else notes,
    )


class _Client:
    def __init__(self, forums=None, errors=None):
        self.forums, self.errors, self.calls = forums or {}, errors or {}, []

    def get_notes(self, forum=None, limit=None):
        self.calls.append(forum)
        if forum in self.errors:
            raise self.errors[forum]
        return [SimpleNamespace(**note) for note in self.forums.get(forum, [])]


@pytest.fixture
def no_pandoc(monkeypatch):
    """Record ODT conversions instead of running pandoc; never sleep."""
    made = []
    monkeypatch.setattr(cli, "_ensure_pandoc", lambda: None)
    monkeypatch.setattr(
        cli, "markdown_to_odt",
        lambda text, path, quiet=False: (made.append(path), open(path, "w").close()),
    )
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    return made


class TestSubmissionPaths:
    def test_readable_and_safe(self, tmp_path):
        sub = _sub(forum="aB3-_x", number=42, title="Is it: safe? / yes\\no <ok>")
        md, odt = cli.submission_paths(sub, tmp_path)
        assert md == tmp_path / "Conf.cc-2026-Conference" / "42_Is-it-safe-yes-no-ok_aB3-_x.md"
        assert odt.suffix == ".odt" and odt.with_suffix(".md") == md

    def test_title_without_ascii_and_no_number(self, tmp_path):
        md, _ = cli.submission_paths(_sub(forum="f", number=None, title="机器学习", venue=""), tmp_path)
        assert md == tmp_path / "unknown-venue" / "f.md"

    def test_long_title_is_capped(self, tmp_path):
        md, _ = cli.submission_paths(_sub(title="word " * 50), tmp_path)
        assert len(md.name) < 80


class TestDownloadSubmissions:
    def test_writes_markdown_and_odt_without_requests(self, tmp_path, no_pandoc, capsys):
        client = _Client()
        code = cli.download_submissions(client, [_sub("f1", 1), _sub("f2", 2, role="Reviewer")], tmp_path)
        assert code == 0 and client.calls == []
        files = sorted(p.name for p in (tmp_path / "Conf.cc-2026-Conference").iterdir())
        assert files == ["1_A-title_f1.md", "1_A-title_f1.odt", "2_A-title_f2.md", "2_A-title_f2.odt"]
        text = (tmp_path / "Conf.cc-2026-Conference" / "2_A-title_f2.md").read_text(encoding="utf-8")
        assert "## Program Chairs" in text  # reviewers get the decision too
        out = capsys.readouterr().out
        assert "[2/2] Reviewer" in out and "Saved 2 of 2" in out

    def test_rerun_replaces_files_and_drops_the_old_title(self, tmp_path, no_pandoc):
        cli.download_submissions(_Client(), [_sub(title="Old title")], tmp_path)
        cli.download_submissions(_Client(), [_sub(title="New title")], tmp_path)
        cli.download_submissions(_Client(), [_sub(title="New title")], tmp_path)
        files = sorted(p.name for p in (tmp_path / "Conf.cc-2026-Conference").iterdir())
        assert files == ["1_New-title_f1.md", "1_New-title_f1.odt"]

    def test_forum_is_fetched_when_replies_were_not_listed(self, tmp_path, no_pandoc):
        client = _Client(forums={"f1": _forum("f1")})
        assert cli.download_submissions(client, [_sub(notes=None)], tmp_path) == 0
        assert client.calls == ["f1"]

    def test_one_failure_does_not_stop_the_batch(self, tmp_path, no_pandoc, capsys, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted during a batch"))
        forbidden = Exception({"name": "ForbiddenError", "message": "no access", "status": 403})
        client = _Client(forums={"f3": _forum("f3")}, errors={"f1": forbidden})
        subs = [_sub("f1", 1, notes=None), _sub("f2", 2, notes=[]), _sub("f3", 3, notes=None)]
        assert cli.download_submissions(client, subs, tmp_path) == 1
        out = capsys.readouterr().out
        assert "Saved 1 of 3" in out
        assert "ForbiddenError: no access" in out and "nothing readable" in out

    def test_stops_after_repeated_failures(self, tmp_path, no_pandoc, capsys):
        client = _Client(errors={f"f{i}": RuntimeError("down") for i in range(10)})
        subs = [_sub(f"f{i}", i, notes=None) for i in range(10)]
        assert cli.download_submissions(client, subs, tmp_path) == 1
        assert client.calls == ["f0", "f1", "f2"]
        assert "Stopped early" in capsys.readouterr().out

    def test_rejected_session_stops_at_once(self, tmp_path, no_pandoc, capsys):
        expired = Exception({"name": "TokenExpiredError", "message": "expired", "status": 401})
        client = _Client(errors={"f0": expired})
        subs = [_sub(f"f{i}", i, notes=None) for i in range(4)]
        assert cli.download_submissions(client, subs, tmp_path) == 1
        assert client.calls == ["f0"]

    def test_odt_failure_keeps_the_markdown(self, tmp_path, monkeypatch, capsys):
        def locked(text, path, quiet=False):
            raise PermissionError("in use")

        monkeypatch.setattr(cli, "_ensure_pandoc", lambda: None)
        monkeypatch.setattr(cli, "markdown_to_odt", locked)
        assert cli.download_submissions(_Client(), [_sub()], tmp_path) == 0
        assert (tmp_path / "Conf.cc-2026-Conference" / "1_A-title_f1.md").exists()
        assert "Markdown only" in capsys.readouterr().out

    def test_without_pandoc_only_markdown_is_written(self, tmp_path, monkeypatch, capsys):
        def missing():
            raise OSError("no pandoc")

        monkeypatch.setattr(cli, "_ensure_pandoc", missing)
        monkeypatch.setattr(cli, "markdown_to_odt", lambda *a, **k: pytest.fail("converted"))
        assert cli.download_submissions(_Client(), [_sub()], tmp_path) == 0
        assert "Markdown files only" in capsys.readouterr().out

    def test_ctrl_c_is_passed_on(self, tmp_path, no_pandoc, capsys):
        client = _Client(errors={"f1": KeyboardInterrupt()})
        with pytest.raises(KeyboardInterrupt):
            cli.download_submissions(client, [_sub(notes=None)], tmp_path)
        assert "Interrupted after 0 of 1" in capsys.readouterr().out


class TestFilterByRole:
    @pytest.mark.parametrize("wanted", ["reviewer", "Reviewers", "REVIEWER"])
    def test_spellings(self, wanted):
        subs = [_sub("a", 1, role="Author"), _sub("r", 2, role="Reviewer")]
        assert [s.forum for s in cli.filter_by_role(subs, [wanted])] == ["r"]

    @pytest.mark.parametrize("wanted", ["area-chair", "Area Chairs", "ac", "area_chair"])
    def test_area_chair_spellings(self, wanted):
        subs = [_sub("a", 1, role="Senior Area Chair"), _sub("c", 2, role="Area Chair")]
        assert [s.forum for s in cli.filter_by_role(subs, [wanted])] == ["c"]

    def test_matching_role_becomes_the_listed_one(self):
        sub = _sub()
        sub.roles = ["Author", "Reviewer"]
        (kept,) = cli.filter_by_role([sub], ["reviewer"])
        assert kept.role == "Reviewer"


class TestBulkCommand:
    @pytest.fixture
    def listed(self, monkeypatch, tmp_path, no_pandoc):
        """Logged in, with two submissions and one unreadable paper on the account."""
        subs = [_sub("f1", 1, title="Mine"), _sub("f2", 2, title="Reviewed", role="Reviewer")]
        unresolved = [Unresolved(V, 77, ["Author"])]
        client = _Client()
        monkeypatch.setattr(cli, "resolve_client", lambda args, port: client)
        monkeypatch.setattr(discover, "list_submissions", lambda c: (list(subs), list(unresolved)))
        monkeypatch.chdir(tmp_path)
        return subs

    def _main(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["openreview-dl", *argv])
        return cli.main()

    def test_all_downloads_everything_into_output(self, monkeypatch, listed, tmp_path, capsys):
        assert self._main(monkeypatch, "--all") == 0
        names = sorted(p.name for p in (tmp_path / "output" / "Conf.cc-2026-Conference").glob("*.md"))
        assert names == ["1_Mine_f1.md", "2_Reviewed_f2.md"]
        out = capsys.readouterr().out
        assert f"{V} #77 (Author)" in out  # the unreadable paper is mentioned

    def test_role_and_output_dir(self, monkeypatch, listed, tmp_path):
        assert self._main(monkeypatch, "--all", "--role", "reviewer", "--output-dir", "elsewhere") == 0
        names = [p.name for p in (tmp_path / "elsewhere").rglob("*.md")]
        assert names == ["2_Reviewed_f2.md"]
        assert not (tmp_path / "output").exists()

    def test_unknown_role_lists_the_real_ones(self, monkeypatch, listed, capsys):
        assert self._main(monkeypatch, "--all", "--role", "chef") == 1
        out = capsys.readouterr().out
        assert "No submissions with role chef" in out and "Your roles: Author, Reviewer." in out

    def test_list_prints_and_writes_nothing(self, monkeypatch, listed, tmp_path, capsys):
        assert self._main(monkeypatch, "--list") == 0
        out = capsys.readouterr().out
        assert f"Author · {V} (1)" in out and f"Reviewer · {V} (1)" in out
        assert "f2  [Under review]  Reviewed" in out and "2 submission(s)." in out
        assert not (tmp_path / "output").exists()

    def test_pick_downloads_the_selection(self, monkeypatch, listed, tmp_path):
        monkeypatch.setattr(cli.picker, "can_run_picker", lambda: True)
        monkeypatch.setattr(cli.picker, "pick", lambda subs: [subs[1]])
        assert self._main(monkeypatch, "--pick") == 0
        assert [p.name for p in (tmp_path / "output").rglob("*.md")] == ["2_Reviewed_f2.md"]

    def test_pick_cancelled_downloads_nothing(self, monkeypatch, listed, tmp_path, capsys):
        monkeypatch.setattr(cli.picker, "can_run_picker", lambda: True)
        monkeypatch.setattr(cli.picker, "pick", lambda subs: None)
        assert self._main(monkeypatch, "--pick") == 0
        assert "Nothing selected." in capsys.readouterr().out
        assert not (tmp_path / "output").exists()

    def test_pick_without_a_terminal_explains_before_logging_in(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.picker, "can_run_picker", lambda: False)
        monkeypatch.setattr(cli, "resolve_client", lambda *a: pytest.fail("logged in"))
        assert self._main(monkeypatch, "--pick") == 1
        assert "--all" in capsys.readouterr().out

    def test_login_failure_exits(self, monkeypatch):
        monkeypatch.setattr(cli, "resolve_client", lambda args, port: None)
        assert self._main(monkeypatch, "--all") == 1

    def test_no_submissions(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "resolve_client", lambda args, port: _Client())
        monkeypatch.setattr(discover, "list_submissions", lambda c: ([], []))
        assert self._main(monkeypatch, "--list") == 1
        assert "No submissions found" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "argv",
        [["--all", "--pick"], ["--all", "--auth"], ["--role", "author"], ["--output-dir", "x"],
         ["--list", "--wipe-credentials"]],
    )
    def test_conflicting_flags_are_rejected(self, monkeypatch, argv):
        with pytest.raises(SystemExit) as error:
            self._main(monkeypatch, *argv)
        assert error.value.code == 2

    def test_blank_url_opens_the_selector(self, monkeypatch, listed, tmp_path):
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr(cli.picker, "can_run_picker", lambda: True)
        monkeypatch.setattr(cli.picker, "pick", lambda subs: [subs[0]])
        assert self._main(monkeypatch) == 0
        assert [p.name for p in (tmp_path / "output").rglob("*.md")] == ["1_Mine_f1.md"]

    def test_blank_url_without_a_terminal_keeps_the_old_error(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr(cli.picker, "can_run_picker", lambda: False)
        assert self._main(monkeypatch) == 1
        assert "Couldn't extract forum ID" in capsys.readouterr().out


class TestReviewFixes:
    def test_ties_in_creation_time_do_not_depend_on_input_order(self):
        notes = _forum()
        for note in notes:
            note["cdate"] = 5
        assert cli.generate_markdown(notes, include_all=True) == cli.generate_markdown(
            notes[::-1], include_all=True
        )

    def test_notes_replying_to_each_other_are_still_rendered(self):
        sub = f"{V}/Submission1"
        loop = [
            {"id": "c1", "forum": "p", "replyto": "c2", "number": 7, "cdate": 10,
             "signatures": [f"{sub}/Authors"], "content": {}},
            {"id": "c2", "forum": "p", "replyto": "c1", "number": 8, "cdate": 11,
             "signatures": [f"{sub}/Authors"], "content": {}},
            {"id": "self", "forum": "p", "replyto": "self", "number": 9, "cdate": 12,
             "signatures": [f"{sub}/Authors"], "content": {}},
        ]
        md = cli.generate_markdown(_forum() + loop, include_all=True)
        for note_id in ("c1", "c2", "self", "r1", "dec"):
            assert md.count(f"ID: {note_id})") == 1
        assert "## Other notes" in md

    def test_senior_committee_signers_are_not_called_reviewers(self):
        notes = _forum()
        notes[2]["signatures"] = [f"{V}/Submission1/Senior_Program_Committee_zzzz"]
        md = cli.generate_markdown(notes, include_all=True)
        assert "## Senior Program Committee zzzz" in md and "## Reviewer zzzz" not in md

    def test_deep_replies_say_what_they_answer(self):
        notes = [{"id": "p", "replyto": None, "content": {}}]
        parent = "p"
        for i in range(6):
            notes.append({"id": f"n{i}", "replyto": parent, "number": i, "cdate": i,
                          "signatures": [f"{V}/Submission1/Reviewer_aaaa"], "content": {}})
            parent = f"n{i}"
        md = cli.generate_markdown(notes, include_all=True)
        assert "*In reply to Reviewer aaaa (ID: n4)*" in md
        assert "(ID: n0)*" not in md  # shallow replies are clear from the heading level

    @pytest.mark.parametrize("wanted, role", [("ACs", "Area Chair"), ("SACs", "Senior Area Chair"),
                                               ("AEs", "Action Editor"), ("authors", "Author")])
    def test_plural_aliases(self, wanted, role):
        assert [s.forum for s in cli.filter_by_role([_sub(role=role)], [wanted])] == ["f1"]

    def test_control_characters_in_titles_are_not_printed(self, capsys):
        cli.print_listing([_sub(title="Nice\x1b]0;x\x07 paper‮")])
        out = capsys.readouterr().out
        assert "\x1b" not in out and "\x07" not in out and "‮" not in out

    def test_failed_write_keeps_the_files_of_an_earlier_run(self, tmp_path, no_pandoc, monkeypatch):
        cli.download_submissions(_Client(), [_sub(title="Old title")], tmp_path)

        def full(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(cli.os, "replace", full)
        assert cli.download_submissions(_Client(), [_sub(title="New title")], tmp_path) == 1
        names = {p.name for p in (tmp_path / "Conf.cc-2026-Conference").iterdir()}
        assert {"1_Old-title_f1.md", "1_Old-title_f1.odt"} <= names

    def _main(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["openreview-dl", *argv])
        return cli.main()

    @pytest.mark.parametrize("argv", [["--all", "--role", ""], ["--all", "--role", ","],
                                      ["--all", "--output-dir", ""]])
    def test_empty_values_are_rejected(self, monkeypatch, argv):
        with pytest.raises(SystemExit) as error:
            self._main(monkeypatch, *argv)
        assert error.value.code == 2

    def test_failed_listing_exits_without_offering_to_delete_the_login(self, monkeypatch, capsys):
        forbidden = Exception({"name": "ForbiddenError", "message": "no", "status": 403})
        monkeypatch.setattr(cli, "resolve_client", lambda args, port: _Client())
        monkeypatch.setattr(discover, "list_submissions", lambda c: (_ for _ in ()).throw(forbidden))
        monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted"))
        assert self._main(monkeypatch, "--all") == 1
        assert "Could not list your submissions: ForbiddenError: no" in capsys.readouterr().out

    def test_role_held_only_on_unreadable_papers(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "resolve_client", lambda args, port: _Client())
        monkeypatch.setattr(
            discover, "list_submissions",
            lambda c: ([_sub()], [Unresolved(V, 77, ["Reviewer"])]),
        )
        assert self._main(monkeypatch, "--list", "--role", "reviewer") == 1
        out = capsys.readouterr().out
        assert "Your roles: Author, Reviewer." in out and f"{V} #77 (Reviewer)" in out

    def test_selector_failure_is_reported(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "resolve_client", lambda args, port: _Client())
        monkeypatch.setattr(discover, "list_submissions", lambda c: ([_sub()], []))
        monkeypatch.setattr(cli.picker, "can_run_picker", lambda: True)
        monkeypatch.setattr(cli.picker, "pick", lambda subs: (_ for _ in ()).throw(RuntimeError("it crashed")))
        assert self._main(monkeypatch, "--pick") == 1
        assert "could not run: it crashed" in capsys.readouterr().out

    def test_output_that_the_console_cannot_encode_does_not_abort(self, monkeypatch, tmp_path, no_pandoc):
        import io

        monkeypatch.setattr(cli, "resolve_client", lambda args, port: _Client())
        monkeypatch.setattr(discover, "list_submissions", lambda c: ([_sub(title="机器学习 ✓")], []))
        raw = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii"))
        monkeypatch.chdir(tmp_path)
        assert self._main(monkeypatch, "--all") == 0
        sys.stdout.flush()
        assert b"Saved 1 of 1" in raw.getvalue()
