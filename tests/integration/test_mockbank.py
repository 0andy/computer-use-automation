"""Phase 0 contracts for the MockBank demo target, exercised over HTTP with the stdlib
against the live ``mockbank_server`` fixture (docs/spec.md sections 4 and 17).
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

import pytest

MEMBER_ID = "12345"
KNOWN_SAVINGS = "$1,234.56"
MISSING_MEMBER_ID = "99999"


# --- a tiny browser-like client (cookie-less; every instance is an independent client) ---


@dataclass(frozen=True)
class Response:
    status: int
    url: str  # final URL after redirects
    body: str
    headers: dict[str, str]

    @property
    def path(self) -> str:
        return urllib.parse.urlsplit(self.url).path


class _KeepMethodOnRedirect(urllib.request.HTTPRedirectHandler):
    """Follow 307/308 the way a browser does: re-send the same method and body."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # urllib hook
        if code in (307, 308):
            return urllib.request.Request(newurl, data=req.data, method=req.get_method())
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Client:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._opener = urllib.request.build_opener(_KeepMethodOnRedirect())

    def get(self, path: str) -> Response:
        return self._send(urllib.request.Request(self.base_url + path, method="GET"))

    def post(self, path: str, form: dict[str, str]) -> Response:
        data = urllib.parse.urlencode(form).encode("utf-8")
        return self._send(urllib.request.Request(self.base_url + path, data=data, method="POST"))

    def submit(self, form: Form, **overrides: str) -> Response:
        assert form.method == "post", f"form {form.action!r} is not a POST form"
        return self.post(form.action, {**form.fields, **overrides})

    def _send(self, request: urllib.request.Request) -> Response:
        try:
            with self._opener.open(request, timeout=10) as resp:
                return Response(resp.status, resp.geturl(), resp.read().decode("utf-8"), dict(resp.headers))
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")
            return Response(err.code, err.geturl(), body, dict(err.headers))


# --- minimal HTML helpers (stdlib html.parser) ---


def _normalize(text: str) -> str:
    return " ".join(text.split())


class _TableRows(HTMLParser):
    """Every <tr> as a list of normalized cell texts (innermost table wins on nesting)."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_normalize("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def table_rows(html: str) -> list[list[str]]:
    parser = _TableRows()
    parser.feed(html)
    parser.close()
    return parser.rows


@dataclass
class Form:
    action: str
    method: str
    fields: dict[str, str] = field(default_factory=dict)  # hidden/text inputs, name -> value
    submits: list[str] = field(default_factory=list)  # visible submit labels


class _Forms(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[Form] = []
        self._form: Form | None = None
        self._button: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._form = Form(action=a.get("action") or "", method=(a.get("method") or "get").lower())
        elif self._form is not None and tag == "input":
            if (a.get("type") or "text") == "submit":
                self._form.submits.append(a.get("value") or "Submit")
            elif a.get("name"):
                self._form.fields[a["name"]] = a.get("value") or ""
        elif self._form is not None and tag == "button" and (a.get("type") or "submit") == "submit":
            self._button = []

    def handle_data(self, data):
        if self._button is not None:
            self._button.append(data)

    def handle_endtag(self, tag):
        if tag == "button" and self._button is not None and self._form is not None:
            self._form.submits.append(_normalize("".join(self._button)))
            self._button = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None


def form_with_submit(html: str, label: str) -> Form:
    parser = _Forms()
    parser.feed(html)
    parser.close()
    matches = [f for f in parser.forms if label in f.submits]
    assert len(matches) == 1, f"expected exactly one form with a {label!r} submit, found {len(matches)}"
    return matches[0]


def has_row(html: str, first_cell: str) -> bool:
    return any(row and row[0] == first_cell for row in table_rows(html))


# --- flows ---


def search(client: Client, member_id: str) -> Response:
    """Open Members, fill Member ID, press Search (a POST to /member)."""
    form = form_with_submit(client.get("/members").body, "Search")
    assert form.method == "post" and form.action == "/member"
    return client.submit(form, member_id=member_id)


def arm(client: Client, fault: str) -> Response:
    """Arm ``<fault>=once`` through the out-of-band /settings page."""
    settings = client.get("/settings")
    form = form_with_submit(settings.body, f"Arm {fault}=once")
    return client.submit(form)


def armed_state(client: Client) -> dict[str, str]:
    rows = table_rows(client.get("/settings").body)
    return {row[0]: row[1] for row in rows if len(row) >= 2 and row[0] in ("interstitial", "unknown")}


def assert_member_detail(response: Response) -> None:
    assert response.status == 200
    assert response.path == "/member"
    rows = table_rows(response.body)
    assert ["Member ID", MEMBER_ID] in rows
    assert ["Savings", KNOWN_SAVINGS] in rows
    assert "Close Account" in response.body


@pytest.fixture
def client(mockbank_server) -> Client:
    return Client(mockbank_server.base_url)


# --- tests ---


def test_mockbank_fixture_starts_and_serves_frame_shell(client: Client) -> None:
    shell = client.get("/")
    assert shell.status == 200
    assert "<iframe" in shell.body
    assert 'src="/members"' in shell.body

    members = client.get("/members")
    assert members.status == 200
    form = form_with_submit(members.body, "Search")
    assert form.method == "post"
    assert form.action == "/member"


def test_valid_member_reaches_member_detail_through_post(client: Client) -> None:
    response = search(client, MEMBER_ID)
    assert_member_detail(response)
    assert "set-cookie" not in {k.lower() for k in response.headers}


def test_missing_member_shows_no_member_found(client: Client) -> None:
    response = search(client, MISSING_MEMBER_ID)
    assert response.status == 200
    assert response.path == "/member"
    assert "No member found" in response.body
    assert not has_row(response.body, "Savings")


def test_member_id_never_enters_url(client: Client) -> None:
    visited: list[str] = []

    visited.append(search(client, MEMBER_ID).url)

    arm(client, "interstitial")
    notice = search(client, MEMBER_ID)
    visited.append(notice.url)
    visited.append(client.submit(form_with_submit(notice.body, "Continue")).url)

    for url in visited:
        assert MEMBER_ID not in url

    # No GET lookup exists: the only way to a Member Detail is the POST search.
    assert client.get(f"/member?member_id={MEMBER_ID}").status == 405
    via_query = client.get(f"/members?member_id={MEMBER_ID}")
    assert via_query.status == 200
    assert not has_row(via_query.body, "Savings")


def test_settings_arms_one_shot_interstitial(client: Client, mockbank_server) -> None:
    arm(client, "interstitial")
    assert armed_state(client)["interstitial"] == "yes"

    # Process-wide: a search from a different client consumes the fault.
    other = Client(mockbank_server.base_url)
    notice = search(other, MEMBER_ID)
    assert notice.status == 200
    assert notice.path == "/notice"
    assert "System notice" in notice.body
    assert MEMBER_ID not in notice.url
    # A separate page, not an overlay: no Member Detail rows are present.
    assert not has_row(notice.body, "Savings")
    assert not has_row(notice.body, "Member ID")

    proceed = other.submit(form_with_submit(notice.body, "Continue"))
    assert_member_detail(proceed)
    assert armed_state(client)["interstitial"] == "no"


def test_settings_arms_one_shot_unknown(client: Client, mockbank_server) -> None:
    arm(client, "unknown")
    assert armed_state(client)["unknown"] == "yes"

    other = Client(mockbank_server.base_url)
    override = search(other, MEMBER_ID)
    assert override.status == 200
    assert override.path == "/override"
    assert "Supervisor override required" in override.body
    assert MEMBER_ID not in override.url
    assert not has_row(override.body, "Savings")
    assert not has_row(override.body, "Member ID")

    proceed = other.submit(form_with_submit(override.body, "Acknowledge"))
    assert_member_detail(proceed)
    assert armed_state(client)["unknown"] == "no"


def test_interstitial_is_consumed_once(client: Client) -> None:
    arm(client, "interstitial")
    first = search(client, MEMBER_ID)
    assert first.path == "/notice"

    second = search(client, MEMBER_ID)  # a fresh search, without pressing Continue
    assert_member_detail(second)
    assert armed_state(client)["interstitial"] == "no"


def test_unknown_is_consumed_once(client: Client) -> None:
    arm(client, "unknown")
    first = search(client, MEMBER_ID)
    assert first.path == "/override"

    second = search(client, MEMBER_ID)
    assert_member_detail(second)
    assert armed_state(client)["unknown"] == "no"
