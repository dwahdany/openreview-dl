"""Find every submission the logged-in user can reach on OpenReview (API v2).

Two sources are combined, the same ones the OpenReview web consoles use:

* notes that list the user as an author (``content.authorids``), and
* the per-paper groups the user is a member of, such as
  ``Venue/Submission12/Reviewers`` or ``Venue/Submission12/Area_Chairs``,
  which are turned into submissions by looking the paper numbers up.

Replies are fetched together with the submissions (``details=replies``), so
downloading a listed submission needs no further request. That matters:
OpenReview allows a logged-in user only a few hundred requests per hour.

Venues that still run on the legacy API v1 are not covered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ROLE_ORDER = (
    "Author",
    "Reviewer",
    "Ethics Reviewer",
    "Area Chair",
    "Secondary Area Chair",
    "Senior Area Chair",
    "Action Editor",
)

# Last segment of a per-paper group id (lower-cased) -> role shown to the user.
_ROLE_LABELS = {
    "authors": "Author",
    "reviewers": "Reviewer",
    "program_committee": "Reviewer",  # AAAI
    "ethics_reviewers": "Ethics Reviewer",
    "area_chairs": "Area Chair",
    "areachairs": "Area Chair",
    "senior_program_committee": "Area Chair",  # AAAI
    "secondary_area_chairs": "Secondary Area Chair",
    "senior_area_chairs": "Senior Area Chair",
    "action_editors": "Action Editor",
}

# Groups that grant (or deny) read access without being an assignment.
_NOT_A_ROLE = re.compile(r"(?i)nonreaders$|readers$|^Solicit_")
_PAPER_SEGMENT = re.compile(r"^([A-Za-z][A-Za-z_-]*?)(\d+)$")
# Anonymous per-paper identities look like Reviewer_aBc1 / Area_Chair_x9Yz.
_ANON_SUFFIX = re.compile(r"_[A-Za-z0-9]{4}$")

# Records imported from DBLP/arXiv/ORCID or uploaded to the archive carry the
# user as an author but have no review forum.
_NO_REVIEWS = (
    "dblp.org",
    "openreview.net/public_article",
    "openreview.net/archive",
    "openreview.net/anonymous_preprint",
)

_STATUS = {
    "submission": "Under review",
    "rejected_submission": "Rejected",
    "withdrawn_submission": "Withdrawn",
    "desk_rejected_submission": "Desk rejected",
    # journals
    "submitted": "Submitted",
    "under_review": "Under review",
    "rejected": "Rejected",
    "desk_rejected": "Desk rejected",
}

_CHUNK = 50  # paper numbers per request


@dataclass
class Submission:
    """One submission the user can open, with what is needed to list and save it."""

    forum: str
    number: int | None
    title: str
    venue_id: str  # e.g. ICLR.cc/2026/Conference
    status: str  # "Under review", "Rejected", "ICLR 2026 Poster", ...
    roles: list[str] = field(default_factory=list)
    cdate: int = 0
    # The submission note followed by its replies, as dicts; None if not fetched.
    notes: list[dict] | None = field(default=None, repr=False)

    @property
    def role(self) -> str:
        return self.roles[0] if self.roles else "Other"

    @property
    def venue(self) -> str:
        return self.venue_id


@dataclass
class Unresolved:
    """A paper the user holds a role on whose submission could not be read."""

    venue_id: str
    number: int
    roles: list[str]


def _value(field_):
    """The value of an API v2 content field (``{"value": x}``), or the field itself."""
    if isinstance(field_, dict) and "value" in field_:
        return field_["value"]
    return field_


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def role_label(segment: str) -> str:
    """Readable role for the last segment of a per-paper group id."""
    key = segment.lower()
    if key in _ROLE_LABELS:
        return _ROLE_LABELS[key]
    stem = _ANON_SUFFIX.sub("", segment).lower()  # Reviewer_aBc1 -> reviewer
    for candidate in (stem, stem + "s"):
        if candidate in _ROLE_LABELS:
            return _ROLE_LABELS[candidate]
    words = _ANON_SUFFIX.sub("", segment).replace("_", " ").strip()
    return words[:-1] if words.endswith("s") and len(words) > 3 else words


def role_rank(role: str) -> int:
    return ROLE_ORDER.index(role) if role in ROLE_ORDER else len(ROLE_ORDER)


def parse_paper_group(group_id, domain):
    """``(venue_id, paper_token, number, role_segment)`` for a per-paper role group.

    ``domain`` is the venue id OpenReview stores on the group. Returns None for
    anything else: venue-wide groups (``Venue/Reviewers``), sub-groups
    (``.../Reviewers/Submitted``), reader lists and groups without a domain
    (legacy API v1 venues).
    """
    if not group_id or not domain or not group_id.startswith(domain + "/"):
        return None
    segments = group_id[len(domain) + 1:].split("/")
    if len(segments) != 2:
        return None
    match = _PAPER_SEGMENT.match(segments[0])
    if not match or not segments[1] or _NOT_A_ROLE.search(segments[1]):
        return None
    return domain, match.group(1), int(match.group(2)), segments[1]


def status_text(venue_id: str, venueid, venue) -> str:
    """Short state of a submission from its ``venueid`` and ``venue`` fields."""
    venueid, venue = _text(venueid), _text(venue)
    if venue_id and venueid.startswith(venue_id + "/"):
        suffix = venueid[len(venue_id) + 1:]
        return _STATUS.get(suffix.lower()) or suffix.replace("_", " ")
    # venueid == venue id once a paper is accepted; ``venue`` then reads
    # "ICLR 2026 Poster". Show what OpenReview shows.
    return venue or venueid or ""


def _invitations(note) -> list:
    invitations = getattr(note, "invitations", None)
    if invitations:
        return list(invitations)
    invitation = getattr(note, "invitation", None)
    return [invitation] if invitation else []


def has_no_review_forum(note) -> bool:
    """True for DBLP/arXiv imports and archive uploads."""
    content = getattr(note, "content", None) or {}
    ids = _invitations(note) + [_value(content.get("venueid")), getattr(note, "domain", None)]
    for candidate in ids:
        low = str(candidate or "").lower()
        if any(low == root or low.startswith(root + "/") for root in _NO_REVIEWS):
            return True
    return bool(getattr(note, "external_ids", None))


def _note_dict(note) -> dict:
    """The fields the Markdown renderer reads, from a Note object."""
    return {
        "id": note.id,
        "forum": getattr(note, "forum", None),
        "replyto": getattr(note, "replyto", None),
        "number": getattr(note, "number", None),
        "cdate": getattr(note, "cdate", None),
        "signatures": getattr(note, "signatures", None),
        "invitations": _invitations(note),
        "content": getattr(note, "content", None),
    }


def _submission(note, roles) -> Submission:
    content = getattr(note, "content", None) or {}
    invitations = _invitations(note)
    venue_id = getattr(note, "domain", None) or (
        invitations[0].split("/-/")[0] if invitations else ""
    )
    details = getattr(note, "details", None) or {}
    replies = details.get("replies") if isinstance(details, dict) else None
    notes = None
    if isinstance(replies, list) and all(
        isinstance(r, dict) and r.get("id") and "content" in r for r in replies
    ):
        notes = [_note_dict(note)] + replies
    return Submission(
        forum=note.id,
        number=getattr(note, "number", None),
        title=_text(_value(content.get("title"))) or "(untitled)",
        venue_id=venue_id,
        status=status_text(venue_id, _value(content.get("venueid")), _value(content.get("venue"))),
        roles=list(roles),
        cdate=getattr(note, "cdate", None) or 0,
        notes=notes,
    )


def own_profile_id(client):
    """The ~id of the logged-in user, or None."""
    profile = getattr(client, "profile", None)
    profile_id = getattr(profile, "id", None)
    if profile_id:
        return profile_id
    try:
        return client.get_profile().id
    except Exception:
        return None


def _member_groups(client, profile_id):
    """``[(group id, domain)]`` for every group the user is a member of."""
    response = None
    try:
        # One request; the library's get_groups cannot ask for only these fields.
        response = client.session.get(
            client.groups_url,
            params={"member": profile_id, "stream": "true", "select": "id,domain"},
            headers=client.headers,
            timeout=60,
        )
        if response.status_code == 200:
            groups = response.json()["groups"]
            return [(g.get("id"), g.get("domain")) for g in groups if isinstance(g, dict)]
    except OSError:  # no network: the library would only fail the same way
        raise
    except Exception:
        pass
    if response is not None and response.status_code in (401, 403, 429):
        try:
            info = response.json()
        except Exception:
            info = None
        if not isinstance(info, dict):
            info = {"name": "Error", "message": "request refused"}
        info.setdefault("status", response.status_code)
        raise RuntimeError(info)  # refused: do not send the same request again
    groups = client.get_all_groups(member=profile_id)
    return [(g.id, getattr(g, "domain", None)) for g in groups]


def _is_root(note) -> bool:
    return getattr(note, "forum", None) == note.id and not getattr(note, "replyto", None)


def _notes_by_number(client, invitation, venue_id, numbers):
    """Submission notes of ``venue_id`` with the given paper numbers, replies included."""
    found = {}
    numbers = sorted(numbers)
    for start in range(0, len(numbers), _CHUNK):
        chunk = numbers[start:start + _CHUNK]
        notes = client.get_notes(
            invitation=invitation,
            number=",".join(map(str, chunk)),
            domain=venue_id,
            details="replies",
        )
        for note in notes:
            if getattr(note, "number", None) in chunk:
                found.setdefault(note.number, note)
    return found


def _submission_invitation(client, venue_id):
    """The venue's submission invitation as configured on its group, or None."""
    try:
        content = getattr(client.get_group(venue_id), "content", None) or {}
    except Exception:
        return None
    return _text(_value(content.get("submission_id"))) or None


def sort_submissions(submissions):
    """Order for display: by role, then newest venue first, then paper number."""
    newest = {}
    for sub in submissions:
        key = (sub.role, sub.venue_id)
        newest[key] = max(newest.get(key, 0), sub.cdate or 0)
    return sorted(
        submissions,
        key=lambda s: (
            role_rank(s.role),
            s.role,
            -newest[(s.role, s.venue_id)],
            s.venue_id,
            s.number if s.number is not None else 0,
            s.forum,
        ),
    )


def list_submissions(client, warn=print):
    """Everything the logged-in user can reach: ``(submissions, unresolved)``.

    ``submissions`` is sorted for display and holds each forum once, with all
    of the user's roles on it. ``unresolved`` lists papers the user holds a
    role on whose submission note could not be read (deleted, not yet visible,
    or a venue with an unusual setup). Problems with one venue are reported
    through ``warn`` and do not stop the listing.
    """
    profile_id = own_profile_id(client)
    if not profile_id:
        raise RuntimeError("could not tell which OpenReview account is logged in")

    by_forum: dict[str, Submission] = {}
    by_paper: dict[tuple, Submission] = {}  # (venue id, number) -> submission

    def add(note, roles):
        sub = by_forum.get(note.id)
        if sub is None:
            sub = by_forum[note.id] = _submission(note, [])
            if sub.number is not None:
                by_paper.setdefault((sub.venue_id, sub.number), sub)
        add_roles(sub, roles)
        return sub

    authored = client.get_all_notes(content={"authorids": profile_id}, details="replies")
    for note in authored:
        if _is_root(note) and not has_no_review_forum(note):
            add(note, ["Author"])

    # (venue id, number) -> {"token": "Submission", "roles": [...]}
    assigned: dict[tuple, dict] = {}
    # Not caught: without the groups every reviewer and committee paper would
    # be missing from a listing that looks complete.
    for group_id, domain in _member_groups(client, profile_id):
        parsed = parse_paper_group(group_id, domain)
        if not parsed:
            continue
        venue_id, token, number, segment = parsed
        entry = assigned.setdefault((venue_id, number), {"token": token, "roles": []})
        label = role_label(segment)
        if label and label not in entry["roles"]:
            entry["roles"].append(label)

    missing: dict[str, dict] = {}  # venue id -> {number: entry}
    for (venue_id, number), entry in assigned.items():
        sub = by_paper.get((venue_id, number))
        if sub is not None:
            add_roles(sub, entry["roles"])
        else:
            missing.setdefault(venue_id, {})[number] = entry

    unresolved = []
    for venue_id, entries in missing.items():
        token = next(iter(entries.values()))["token"]
        found = {}
        try:
            # Journals name the group Paper12 but the invitation Submission.
            guesses = [f"{venue_id}/-/Submission"]
            if token != "Submission":
                guesses.append(f"{venue_id}/-/{token}")
            for invitation in guesses:
                todo = [n for n in entries if n not in found]
                if todo:
                    found.update(_notes_by_number(client, invitation, venue_id, todo))
            todo = [n for n in entries if n not in found]
            if todo:
                configured = _submission_invitation(client, venue_id)
                if configured and configured not in guesses:
                    found.update(_notes_by_number(client, configured, venue_id, todo))
        except Exception as e:
            if _affects_every_venue(e):
                raise  # rate limit, expired login, no network: asking on is pointless
            warn(f"Could not list your papers at {venue_id}: {_reason(e)}")
        for number, entry in entries.items():
            note = found.get(number)
            if note is None:
                unresolved.append(Unresolved(venue_id, number, list(entry["roles"])))
            else:
                add(note, entry["roles"])

    for sub in by_forum.values():
        sub.roles.sort(key=role_rank)
    unresolved.sort(key=lambda u: (u.venue_id, u.number))
    return sort_submissions(by_forum.values()), unresolved


def add_roles(sub: Submission, roles):
    for role in roles:
        if role not in sub.roles:
            sub.roles.append(role)


def _affects_every_venue(error) -> bool:
    """True for a failure the next request would meet as well."""
    args = getattr(error, "args", None) or ()
    info = args[0] if args and isinstance(args[0], dict) else {}
    return (
        info.get("status") in (401, 429)
        or info.get("name") in ("TokenExpiredError", "ChallengeRequiredError", "RateLimitError")
        or isinstance(error, OSError)  # requests' connection errors and timeouts
    )


def _reason(error) -> str:
    """One line describing an exception from openreview-py or requests."""
    args = getattr(error, "args", None) or ()
    if args and isinstance(args[0], dict):
        info = args[0]
        name, message = info.get("name"), info.get("message")
        if name and message:
            return f"{name}: {message}"
        return str(name or message or info)
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
