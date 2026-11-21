"""Tests for openreview_dl.discover: listing what the logged-in user can reach."""

from types import SimpleNamespace

import pytest

from openreview_dl import discover
from openreview_dl.discover import (
    has_no_review_forum,
    list_submissions,
    parse_paper_group,
    role_label,
    status_text,
)

ME = "~Test_User1"
V = "Conf.cc/2026/Conference"


def _note(note_id, number, venue=V, title="T", venueid=None, replies=(), **extra):
    content = {
        "title": {"value": title},
        "venueid": {"value": venueid or f"{venue}/Submission"},
        "venue": {"value": "Conf 2026 Submission"},
    }
    fields = dict(
        id=note_id,
        forum=note_id,
        replyto=None,
        number=number,
        cdate=number,
        signatures=[f"{venue}/Submission{number}/Authors"],
        invitations=[f"{venue}/-/Submission"],
        domain=venue,
        content=content,
        details={"replies": list(replies)},
    )
    fields.update(extra)
    return SimpleNamespace(**fields)


def _reply(reply_id, forum):
    return {"id": reply_id, "forum": forum, "replyto": forum, "number": 1, "cdate": 5,
            "signatures": [f"{V}/Submission1/Reviewer_abcd"], "content": {"summary": {"value": "ok"}}}


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, groups, status=200):
        self.groups, self.status, self.calls = groups, status, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(params)
        return _Response(self.status, {"groups": [{"id": g, "domain": d} for g, d in self.groups]})


class _Client:
    """Stands in for OpenReviewClient: canned notes and groups, recorded calls."""

    groups_url = "https://api2.openreview.net/groups"
    headers = {}

    def __init__(self, authored=(), groups=(), by_invitation=None, group_status=200,
                 submission_id=None, failing=()):
        self.profile = SimpleNamespace(id=ME)
        self.authored = list(authored)
        self.session = _Session(list(groups), group_status)
        self.by_invitation = by_invitation or {}
        self.submission_id = submission_id
        self.failing = set(failing)
        self.note_calls = []
        self.group_calls = []

    def get_all_notes(self, **kwargs):
        assert kwargs == {"content": {"authorids": ME}, "details": "replies"}
        return self.authored

    def get_notes(self, invitation=None, number=None, domain=None, details=None):
        assert details == "replies" and domain  # replies must come along: no request per forum later
        self.note_calls.append((invitation, number))
        if domain in self.failing:
            raise RuntimeError("boom")
        wanted = {int(n) for n in number.split(",")}
        return [n for n in self.by_invitation.get(invitation, []) if n.number in wanted]

    def get_all_groups(self, member=None):
        return [SimpleNamespace(id=g, domain=d) for g, d in self.session.groups]

    def get_group(self, group_id):
        self.group_calls.append(group_id)
        content = {"submission_id": {"value": self.submission_id}} if self.submission_id else {}
        return SimpleNamespace(content=content)


class TestParsePaperGroup:
    @pytest.mark.parametrize(
        "group_id, expected",
        [
            (f"{V}/Submission12/Reviewers", (V, "Submission", 12, "Reviewers")),
            (f"{V}/Submission12/Reviewer_aB3d", (V, "Submission", 12, "Reviewer_aB3d")),
            (f"{V}/Submission7/Area_Chairs", (V, "Submission", 7, "Area_Chairs")),
            (f"{V}/Paper3/Action_Editors", (V, "Paper", 3, "Action_Editors")),
        ],
    )
    def test_per_paper_role_groups(self, group_id, expected):
        assert parse_paper_group(group_id, V) == expected

    @pytest.mark.parametrize(
        "group_id",
        [
            f"{V}/Reviewers",  # venue-wide
            f"{V}/Reviewers/Invited",
            f"{V}/Submission12/Reviewers/Submitted",  # sub-group
            f"{V}/Submission12",  # container
            f"{V}/Submission12/Commitment_Readers",
            f"{V}/paper12/review-nonreaders",
            "Other.cc/2026/Conference/Submission12/Reviewers",  # not under the domain
            "~Test_User1",
            "everyone",
        ],
    )
    def test_everything_else_is_ignored(self, group_id):
        assert parse_paper_group(group_id, V) is None

    def test_group_without_domain_is_ignored(self):
        assert parse_paper_group(f"{V}/Paper12/Reviewers", None) is None

    def test_venue_id_ending_in_a_digit(self):
        venue = "NeurIPS.cc/2021/Track/Round1"
        assert parse_paper_group(f"{venue}/Reviewers", venue) is None
        assert parse_paper_group(f"{venue}/Submission5/Reviewers", venue)[2] == 5


class TestRoleLabel:
    @pytest.mark.parametrize(
        "segment, label",
        [
            ("Authors", "Author"),
            ("Reviewers", "Reviewer"),
            ("Reviewer_aB3d", "Reviewer"),
            ("Area_Chairs", "Area Chair"),
            ("Area_Chair_x9Yz", "Area Chair"),
            ("Senior_Area_Chairs", "Senior Area Chair"),
            ("Program_Committee", "Reviewer"),
            ("Program_Committee_abcd", "Reviewer"),
            ("Senior_Program_Committee", "Area Chair"),
            ("Ethics_Reviewers", "Ethics Reviewer"),
            ("Action_Editors", "Action Editor"),
            ("Shepherds", "Shepherd"),  # unknown role: made readable
        ],
    )
    def test_labels(self, segment, label):
        assert role_label(segment) == label


class TestStatusText:
    @pytest.mark.parametrize(
        "venueid, venue, expected",
        [
            (f"{V}/Submission", "Conf 2026 Submission", "Under review"),
            (f"{V}/Rejected_Submission", "Submitted to Conf 2026", "Rejected"),
            (f"{V}/Withdrawn_Submission", "x", "Withdrawn"),
            (f"{V}/Desk_Rejected_Submission", "x", "Desk rejected"),
            (f"{V}/Some_New_State", "x", "Some New State"),
            (V, "Conf 2026 Poster", "Conf 2026 Poster"),  # accepted: what OpenReview shows
            (None, None, ""),
            ({"odd": 1}, ["x"], ""),
        ],
    )
    def test_status(self, venueid, venue, expected):
        assert status_text(V, venueid, venue) == expected


class TestNoReviewForum:
    def test_dblp_import_and_archive_upload(self):
        dblp = _note("d", 1, venue="DBLP.org", invitations=["DBLP.org/-/Record"])
        archive = _note("a", 2, invitations=["OpenReview.net/Archive/-/Direct_Upload"])
        imported = _note("x", 3, external_ids=["dblp:conf/x/1"])
        assert has_no_review_forum(dblp)
        assert has_no_review_forum(archive)
        assert has_no_review_forum(imported)

    def test_venue_submission(self):
        assert not has_no_review_forum(_note("n", 1))


class TestListSubmissions:
    def test_authored_and_assigned_papers_are_combined(self):
        mine = _note("mine", 1, title="My paper", replies=[_reply("r1", "mine")])
        reviewed = _note("rev", 12, title="Reviewed", replies=[_reply("r2", "rev")])
        client = _Client(
            authored=[mine, _note("d", 9, venue="DBLP.org", invitations=["DBLP.org/-/Record"])],
            groups=[
                (f"{V}/Submission1/Authors", V),
                (f"{V}/Submission12/Reviewers", V),
                (f"{V}/Submission12/Reviewer_abcd", V),
                (f"{V}/Submission12/Reviewers/Submitted", V),
                (f"{V}/Reviewers", V),
                ("everyone", "OpenReview.net"),
                ("gmail.com", None),
            ],
            by_invitation={f"{V}/-/Submission": [reviewed]},
        )
        subs, unresolved = list_submissions(client)

        assert [(s.forum, s.roles, s.title) for s in subs] == [
            ("mine", ["Author"], "My paper"),
            ("rev", ["Reviewer"], "Reviewed"),
        ]
        assert unresolved == []
        # The authored paper is not fetched again; one request covers the venue.
        assert client.note_calls == [(f"{V}/-/Submission", "12")]
        assert client.session.calls == [{"member": ME, "stream": "true", "select": "id,domain"}]
        # Replies come along, ready for the renderer.
        assert [n["id"] for n in subs[1].notes] == ["rev", "r2"]
        assert subs[1].notes[0]["replyto"] is None
        assert subs[1].status == "Under review"

    def test_several_roles_on_one_paper(self):
        mine = _note("mine", 1)
        client = _Client(
            authored=[mine],
            groups=[(f"{V}/Submission1/Authors", V), (f"{V}/Submission1/Area_Chairs", V)],
        )
        subs, _ = list_submissions(client)
        assert len(subs) == 1 and subs[0].roles == ["Author", "Area Chair"]
        assert client.note_calls == []

    def test_unreadable_paper_is_reported_not_dropped_silently(self):
        client = _Client(groups=[(f"{V}/Submission5/Authors", V)], submission_id=f"{V}/-/Submission")
        subs, unresolved = list_submissions(client)
        assert subs == []
        assert [(u.venue_id, u.number, u.roles) for u in unresolved] == [(V, 5, ["Author"])]

    def test_venue_with_a_custom_submission_invitation(self):
        paper = _note("p", 3, invitations=[f"{V}/-/Manuscript"])
        client = _Client(
            groups=[(f"{V}/Paper3/Action_Editors", V)],
            by_invitation={f"{V}/-/Manuscript": [paper]},
            submission_id=f"{V}/-/Manuscript",
        )
        subs, unresolved = list_submissions(client)
        assert [(s.forum, s.roles) for s in subs] == [("p", ["Action Editor"])]
        assert unresolved == []
        assert client.note_calls == [
            (f"{V}/-/Submission", "3"), (f"{V}/-/Paper", "3"), (f"{V}/-/Manuscript", "3"),
        ]
        assert client.group_calls == [V]

    def test_one_failing_venue_does_not_stop_the_listing(self):
        other = "Other.cc/2026/Workshop"
        good = _note("g", 2, venue=other)
        client = _Client(
            groups=[(f"{V}/Submission1/Reviewers", V), (f"{other}/Submission2/Reviewers", other)],
            by_invitation={f"{other}/-/Submission": [good]},
            failing=[V],
        )
        warnings = []
        subs, unresolved = list_submissions(client, warn=warnings.append)
        assert [s.forum for s in subs] == ["g"]
        assert [(u.venue_id, u.number) for u in unresolved] == [(V, 1)]
        assert len(warnings) == 1 and V in warnings[0] and "boom" in warnings[0]

    def test_falls_back_to_the_library_when_the_raw_group_request_fails(self):
        reviewed = _note("rev", 12)
        client = _Client(
            groups=[(f"{V}/Submission12/Reviewers", V)],
            by_invitation={f"{V}/-/Submission": [reviewed]},
            group_status=500,
        )
        subs, _ = list_submissions(client)
        assert [s.forum for s in subs] == ["rev"]

    def test_many_numbers_are_requested_in_chunks(self):
        numbers = list(range(1, 121))
        client = _Client(
            groups=[(f"{V}/Submission{n}/Reviewers", V) for n in numbers],
            by_invitation={f"{V}/-/Submission": [_note(f"n{n}", n) for n in numbers]},
        )
        subs, unresolved = list_submissions(client)
        assert len(subs) == 120 and not unresolved
        assert [len(call[1].split(",")) for call in client.note_calls] == [50, 50, 20]

    def test_replies_of_an_unexpected_shape_are_not_kept(self):
        odd = _note("rev", 12, details={"replies": [{"id": "r"}]})  # no content
        client = _Client(
            groups=[(f"{V}/Submission12/Reviewers", V)],
            by_invitation={f"{V}/-/Submission": [odd]},
        )
        subs, _ = list_submissions(client)
        assert subs[0].notes is None  # the downloader fetches the forum itself

    def test_order_is_by_role_then_newest_venue_then_number(self):
        old, new = "Conf.cc/2024/Conference", "Conf.cc/2026/Conference"
        client = _Client(
            authored=[_note("a2", 20, venue=old, cdate=100), _note("a3", 7, venue=new, cdate=900),
                      _note("a1", 3, venue=new, cdate=800)],
            groups=[(f"{old}/Submission5/Reviewers", old)],
            by_invitation={f"{old}/-/Submission": [_note("r", 5, venue=old, cdate=50)]},
        )
        subs, _ = list_submissions(client)
        assert [s.forum for s in subs] == ["a1", "a3", "a2", "r"]

    def test_unknown_account_is_an_error(self):
        client = _Client()
        client.profile = None
        client.get_profile = lambda: (_ for _ in ()).throw(RuntimeError("no"))
        with pytest.raises(RuntimeError, match="which OpenReview account"):
            list_submissions(client)


def test_reason_reads_openreview_error_dicts():
    error = Exception({"name": "ForbiddenError", "message": "no access", "status": 403})
    assert discover._reason(error) == "ForbiddenError: no access"
    assert discover._reason(ValueError("bad")) == "ValueError: bad"
    assert discover._reason(KeyboardInterrupt()) == "KeyboardInterrupt"


class TestFailuresThatAffectEverything:
    def test_refused_group_listing_is_an_error_and_is_not_repeated(self):
        client = _Client(authored=[_note("mine", 1)], group_status=429)
        client.get_all_groups = lambda member=None: pytest.fail("asked again")
        with pytest.raises(Exception) as error:
            list_submissions(client)
        assert error.value.args[0]["status"] == 429

    def test_rate_limit_on_a_venue_stops_the_listing(self):
        other = "Other.cc/2026/Workshop"
        client = _Client(
            groups=[(f"{V}/Submission1/Reviewers", V), (f"{other}/Submission2/Reviewers", other)],
        )
        limited = Exception({"name": "RateLimitError", "message": "slow down", "status": 429})

        def refuse(**kwargs):
            client.note_calls.append(kwargs["invitation"])
            raise limited

        client.get_notes = refuse
        with pytest.raises(Exception) as error:
            list_submissions(client, warn=lambda message: pytest.fail(message))
        assert error.value is limited and len(client.note_calls) == 1

    def test_network_error_stops_the_listing(self):
        client = _Client(groups=[(f"{V}/Submission1/Reviewers", V)])
        client.session.get = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("offline"))
        client.get_all_groups = lambda member=None: pytest.fail("asked again")
        with pytest.raises(ConnectionError):
            list_submissions(client)
