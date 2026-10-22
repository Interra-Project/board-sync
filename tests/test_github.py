"""Tests of the GraphQL reads and writes, with a fake transport."""

import http.client
import io
import json
import urllib.error

import pytest

from board_sync import github

DONE = {"hasNextPage": False}
MORE = {"hasNextPage": True}


class Resp(io.BytesIO):
    """File-like HTTP response usable as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(*bodies):
    """Return an opener that answers with ``bodies`` in order and records requests."""
    calls = []

    def opener(req, timeout=None):
        calls.append({**json.loads(req.data), "timeout": timeout, "auth": req.get_header("Authorization")})
        body = bodies[len(calls) - 1]
        if isinstance(body, BaseException):
            raise body
        return Resp(json.dumps(body).encode())

    opener.calls = calls
    return opener


def http_error(code, headers=None, body=b""):
    return urllib.error.HTTPError(github.API, code, "x", headers or {}, io.BytesIO(body))


@pytest.fixture
def slept(monkeypatch):
    out = []
    monkeypatch.setattr(github.time, "sleep", out.append)
    return out


def test_post_returns_data_and_sends_token():
    op = opener_for({"data": {"ok": 1}})
    assert github.Client("tok", op).post("q", {"a": 1}) == {"ok": 1}
    assert op.calls == [{"query": "q", "variables": {"a": 1}, "timeout": 60, "auth": "Bearer tok"}]


def test_post_raises_on_errors():
    with pytest.raises(github.GraphQLError):
        github.Client("t", opener_for({"errors": [{"message": "bad"}]})).post("q")


def test_post_retries_server_errors_and_rate_limits(slept):
    assert github.Client("t", opener_for(http_error(502), {"data": {"ok": 2}})).post("q") == {"ok": 2}
    op = opener_for(http_error(403, {"Retry-After": "7"}), http_error(429, {"Retry-After": "999"}), {"data": {"ok": 3}})
    assert github.Client("t", op).post("q") == {"ok": 3}
    op = opener_for(http_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), {"data": {"ok": 5}})
    assert github.Client("t", op).post("q") == {"ok": 5}
    op = opener_for(
        http_error(403, body=b'{"message": "You have exceeded a secondary rate limit"}'), {"data": {"ok": 6}}
    )
    assert github.Client("t", op).post("q") == {"ok": 6}
    op = opener_for({"errors": [{"type": "RATE_LIMITED"}]}, {"data": {"ok": 7}})
    assert github.Client("t", op).post("q") == {"ok": 7}
    assert slept == [5, 7, 120, 60, 60, 60]


@pytest.mark.parametrize(
    "failure",
    [http_error(401), http_error(403, body=b'{"message": "Resource not accessible by integration"}')],
)
def test_post_does_not_retry_refusals(failure, slept):
    op = opener_for(failure)
    with pytest.raises(urllib.error.HTTPError):
        github.Client("t", op).post("q")
    assert len(op.calls) == 1 and slept == []


def test_post_gives_up_after_the_retries(slept):
    op = opener_for(*[http_error(503) for _ in range(3)])
    with pytest.raises(urllib.error.HTTPError):
        github.Client("t", op).post("q")
    assert len(op.calls) == 3
    with pytest.raises(github.GraphQLError):
        github.Client("t", opener_for(*[{"errors": [{"type": "RATE_LIMITED"}]}] * 3)).post("q")
    with pytest.raises(urllib.error.URLError):
        github.Client("t", opener_for(*[urllib.error.URLError("down")] * 3)).post("q")


@pytest.mark.parametrize(
    "failure", [urllib.error.URLError("reset"), TimeoutError(), ConnectionResetError(), http.client.IncompleteRead(b"")]
)
def test_post_retries_network_errors(failure, slept):
    assert github.Client("t", opener_for(failure, {"data": {"ok": 4}})).post("q") == {"ok": 4}


def test_post_accepts_partial_not_found_only_when_asked():
    body = {"data": {"nodes": [None]}, "errors": [{"type": "NOT_FOUND", "message": "gone"}]}
    assert github.Client("t", opener_for(body)).post("q", allow_not_found=True) == {"nodes": [None]}
    with pytest.raises(github.GraphQLError):
        github.Client("t", opener_for(body)).post("q")
    for bad in (
        {"data": {"nodes": [None]}, "errors": [{"type": "FORBIDDEN"}]},
        {"data": {"nodes": [None]}, "errors": [{"type": "NOT_FOUND"}, {"type": "FORBIDDEN"}]},
        {"data": None, "errors": [{"type": "NOT_FOUND"}]},
    ):
        with pytest.raises(github.GraphQLError):
            github.Client("t", opener_for(bad)).post("q", allow_not_found=True)


class Fake:
    """Client stand-in that answers by query name and records mutations and partial-read flags."""

    def __init__(self, pages=None, fields=None, values=None, pid="PID"):
        self.pages, self.fields, self.values = list(pages or []), fields or [], values or []
        self.sent, self.flags, self.pid, self.cursors = [], [], pid, []

    def post(self, query, variables=None, allow_not_found=False):
        if "projectV2(number" in query:
            self.flags.append(("board", allow_not_found))
            self.cursors.append(variables["after"])
            return {"organization": {"projectV2": {**self.pages.pop(0), "id": self.pid}}}
        if "issueFields(first" in query:
            self.flags.append(("issue_fields", allow_not_found))
            return {"organization": {"issueFields": {"pageInfo": DONE, "nodes": self.fields}}}
        if "nodes(ids" in query:
            self.flags.append(("issue_values", allow_not_found))
            found = [v for v in self.values if v and v["id"] in variables["ids"]]
            return {"nodes": found + [None] * (len(variables["ids"]) - len(found))}
        self.sent.append((query.split("{")[1].split("(")[0].strip(), variables))
        return {}


def page(items, more=False, field_id="F1", total=None):
    return {
        "fields": {
            "pageInfo": DONE,
            "nodes": [
                {"id": field_id, "name": "Start date", "dataType": "DATE"},
                {"id": "F2", "name": "Status", "dataType": "SINGLE_SELECT", "options": [{"id": "o1", "name": "TBD"}]},
                {"id": "F3", "name": "Title", "dataType": "TITLE"},
                {},
            ],
        },
        "items": {
            "totalCount": len(items) if total is None else total,
            "pageInfo": {"hasNextPage": more, "endCursor": "C1"},
            "nodes": items,
        },
    }


BOARD_VALUES = {"Start date": ["2026-01-01", "t1"], "Status": ["TBD", "t2"]}


def board_item(iid, org="FormingWorlds"):
    return {
        "id": f"item-{iid}",
        "content": {"id": iid, "url": f"u/{iid}", "repository": {"owner": {"login": org}}},
        "fieldValues": {
            "totalCount": 3,
            "pageInfo": DONE,
            "nodes": [
                {"date": "2026-01-01", "updatedAt": "t1", "field": {"name": "Start date"}},
                {"name": "TBD", "updatedAt": "t2", "field": {"name": "Status"}},
                {},
            ],
        },
    }


def issue_node(iid, values=(), events=()):
    return {
        "id": iid,
        "issueFieldValues": {"totalCount": len(values), "pageInfo": DONE, "nodes": list(values)},
        "timelineItems": {"nodes": list(events)},
    }


def test_read_board_pages_and_skips_drafts():
    draft = {"id": "d", "content": {}, "fieldValues": {"pageInfo": DONE, "nodes": []}}
    fake = Fake(pages=[page([board_item("A"), draft, None], more=True, total=4), page([board_item("B")], total=4)])
    pid, fields, items, skipped = github.read_board(fake, "FormingWorlds", 7)
    assert pid == "PID" and skipped == 2 and fake.cursors == [None, "C1"]
    assert fields["Status"] == {"id": "F2", "type": "select", "options": {"TBD": "o1"}}
    assert fields["Title"]["type"] == "other"
    assert [i[0] for i in items] == ["A", "B"]
    assert items[0] == ("A", "u/A", "FormingWorlds", "item-A", BOARD_VALUES)


def test_read_issue_fields_and_values():
    fake = Fake(
        fields=[
            {"id": "D", "name": "Start date"},
            {"id": "S", "name": "Priority", "options": [{"id": "h", "name": "High"}]},
            {},
        ],
        values=[
            issue_node(
                "A",
                values=[
                    {"value": "2026-02-01", "field": {"name": "Start date"}},
                    {"name": "High", "field": {"name": "Priority"}},
                    {},
                ],
                events=[
                    {"createdAt": "2026-01-01", "issueField": {"name": "Start date"}},
                    {"createdAt": "2026-03-01", "issueField": {"name": "Start date"}},
                    {"createdAt": "2026-02-01", "issueField": {"name": "Start date"}},
                    {"createdAt": "2026-02-01", "issueField": None},
                    None,
                ],
            )
        ],
    )
    assert github.read_issue_fields(fake, "o") == {
        "Start date": {"id": "D", "type": "date", "options": {}},
        "Priority": {"id": "S", "type": "select", "options": {"High": "h"}},
    }
    values, unread = github.read_issue_values(fake, ["A", "GONE"])
    assert values == {"A": {"Start date": ["2026-02-01", "2026-03-01"], "Priority": ["High", None]}}
    assert unread == 1


def test_only_the_issue_values_read_accepts_partial_responses():
    interra = Fake(pages=[page([board_item("X", "Interra-Project")])] * 2, values=[issue_node("X")])
    fw = Fake(pages=[page([None])])
    github.read_all({"Interra-Project": interra, "FormingWorlds": fw})
    assert set(interra.flags + fw.flags) == {
        ("board", github.BOARD_GONE),
        ("issue_fields", False),
        ("issue_values", True),
    }


def test_read_issue_values_batches():
    calls = []

    class C:
        def post(self, q, v, allow_not_found=False):
            calls.append(v["ids"])
            return {"nodes": [None] * len(v["ids"])}

    github.read_issue_values(C(), ["a", "b", "c"], batch=2)
    assert calls == [["a", "b"], ["c"]]


def test_read_all_maps_each_board_to_its_own_project():
    interra = Fake(
        pages=[page([board_item("X", "Interra-Project")]), page([board_item("X", "Interra-Project")])],
        fields=[{"id": "D", "name": "Start date"}],
        values=[issue_node("X")],
        pid="P-interra",
    )
    fw = Fake(pages=[page([board_item("Y"), None], field_id="F7")], pid="P-fw")
    snap = github.read_all({"Interra-Project": interra, "FormingWorlds": fw})
    assert snap["projects"] == {"b1": "P-interra", "b2": "P-interra", "b7": "P-fw"}
    assert snap["fields"]["b7"]["Start date"]["id"] == "F7" and snap["fields"]["b1"]["Start date"]["id"] == "F1"
    assert set(snap["items"]["X"]["places"]) == {"b1", "b2", "issue"}
    assert snap["items"]["X"]["places"]["b2"] == {"item_id": "item-X", "values": BOARD_VALUES}
    assert set(snap["items"]["Y"]["places"]) == {"b7"}  # Y is unreadable in the FormingWorlds issue read
    assert snap["skipped_items"] == {"b1": 0, "b2": 0, "b7": 1}
    assert snap["unread_issues"] == {"Interra-Project": 0, "FormingWorlds": 1}
    assert snap["fields"]["issue:Interra-Project"]["Start date"]["id"] == "D"


def test_removed_issue_field_reads_as_clear_with_time():
    node = issue_node(
        "A",
        values=[{"value": "2026-01-01", "field": {"name": "Start date"}}],
        events=[
            {"__typename": "IssueFieldAddedEvent", "createdAt": "2026-01-01", "issueField": {"name": "Target date"}},
            {"__typename": "IssueFieldRemovedEvent", "createdAt": "2026-02-01", "issueField": {"name": "Target date"}},
            {"__typename": "IssueFieldRemovedEvent", "createdAt": "2026-01-01", "issueField": {"name": "Start date"}},
            {"__typename": "IssueFieldAddedEvent", "createdAt": "2026-03-01", "issueField": {"name": "Start date"}},
        ],
    )
    values, _ = github.read_issue_values(Fake(values=[node]), ["A"])
    assert values == {"A": {"Target date": [None, "2026-02-01"], "Start date": ["2026-01-01", "2026-03-01"]}}


def test_queries_ask_for_the_totals():
    assert "items(first: 100, after: $after) { totalCount pageInfo" in github.BOARD_QUERY
    assert "fieldValues(first: 100) { totalCount pageInfo" in github.BOARD_QUERY
    assert "issueFieldValues(first: 50) { totalCount pageInfo" in github.ISSUE_VALUES_QUERY


@pytest.mark.parametrize("where", ["fields", "values", "issue_fields", "issue_values"])
def test_truncated_lists_stop_the_read(where, slept):
    if where in ("fields", "values"):
        p = page([board_item("A")])
        if where == "fields":
            p["fields"]["pageInfo"] = MORE
        else:
            p["items"]["nodes"][0]["fieldValues"]["pageInfo"] = MORE
        fake = Fake(pages=[p])
        with pytest.raises(github.TruncatedRead):
            github.read_board(fake, "o", 1)
        assert slept == [] and len(fake.cursors) == 1  # not read again
    elif where == "issue_fields":

        class C:
            def post(self, q, v):
                return {"organization": {"issueFields": {"pageInfo": MORE, "nodes": []}}}

        with pytest.raises(github.TruncatedRead):
            github.read_issue_fields(C(), "o")
    else:
        node = issue_node("A")
        node["issueFieldValues"]["pageInfo"] = MORE
        with pytest.raises(github.TruncatedRead):
            github.read_issue_values(Fake(values=[node]), ["A"])


FULL = [board_item("A"), board_item("B")]


@pytest.mark.parametrize(
    "short",
    [
        [page([], total=0)],  # the whole board empty
        [page(FULL[:1], more=True, total=2), page([], total=0)],  # a later page empty
        [page(FULL[:1], more=True, total=2), page([], total=2)],  # fewer items than the total
        [page(FULL[:1], more=True, total=2), page(FULL[1:], total=3)],  # the total changed
        [page([], more=True, total=2), page(FULL[1:], total=2)],  # an empty first page
        [page(FULL + [board_item("C")], total=2)],  # more items than the total
        [page([board_item("A") | {"fieldValues": {"totalCount": 0, "pageInfo": DONE, "nodes": []}}, FULL[1]])],
        [page([board_item("A") | {"fieldValues": {"totalCount": 3, "pageInfo": DONE, "nodes": [{}]}}, FULL[1]])],
        [page([board_item("A") | {"fieldValues": {"totalCount": 1, "pageInfo": DONE, "nodes": [{}] * 3}}, FULL[1]])],
        [page([board_item("A") | {"fieldValues": {"totalCount": 3, "pageInfo": DONE, "nodes": [None] * 3}}, FULL[1]])],
        [page([board_item("A") | {"fieldValues": {"totalCount": 3, "pageInfo": DONE, "nodes": None}}, FULL[1]])],
        [page([board_item("A") | {"fieldValues": None}, FULL[1]])],
    ],
)
def test_short_board_read_is_tried_again(short, slept):
    fake = Fake(pages=[*short, page(FULL)])
    _, _, items, skipped = github.read_board(fake, "Interra-Project", 2)
    assert [i[0] for i in items] == ["A", "B"] and skipped == 0
    assert slept == [github.READ_WAIT]


def test_short_board_read_stops_after_the_attempts(slept):
    fake = Fake(pages=[page([], total=0)] * github.READ_ATTEMPTS)
    with pytest.raises(github.ShortBoardRead, match="0 items read, total 0"):
        github.read_board(fake, "Interra-Project", 1)
    assert (github.READ_ATTEMPTS, github.READ_WAIT) == (6, 10)  # the README states both
    assert len(fake.cursors) == github.READ_ATTEMPTS
    assert slept == [github.READ_WAIT] * (github.READ_ATTEMPTS - 1)


START = {"value": "2026-02-01", "field": {"name": "Start date"}}
SET_START = {"__typename": "IssueFieldAddedEvent", "createdAt": "t1", "issueField": {"name": "Start date"}}


@pytest.mark.parametrize(
    "short",
    [
        {"nodes": []},  # fewer issues than asked for
        {"nodes": [issue_node("A") | {"issueFieldValues": {"totalCount": 1, "pageInfo": DONE, "nodes": []}}]},
        {"nodes": [issue_node("A") | {"issueFieldValues": {"totalCount": 1, "pageInfo": DONE, "nodes": [START] * 2}}]},
        {"nodes": [issue_node("A") | {"issueFieldValues": {"totalCount": 1, "pageInfo": DONE, "nodes": [None]}}]},
        {"nodes": [issue_node("A") | {"issueFieldValues": {"totalCount": 0, "pageInfo": DONE, "nodes": None}}]},
        {"nodes": [issue_node("A") | {"issueFieldValues": None}]},
        {"nodes": [None, None]},  # more issues than asked for
    ],
)
def test_short_issue_read_is_tried_again(short, slept):
    answers = [short, {"nodes": [issue_node("A", values=[START])]}]

    class C:
        def post(self, q, v, allow_not_found=False):
            return answers.pop(0)

    values, unread = github.read_issue_values(C(), ["A"])
    assert values == {"A": {"Start date": ["2026-02-01", None]}} and unread == 0
    assert slept == [github.READ_WAIT]


def test_short_issue_read_stops_after_the_attempts(slept):
    calls = []

    class C:
        def post(self, q, v, allow_not_found=False):
            calls.append(v)
            return {"nodes": []}

    with pytest.raises(github.ShortIssueRead, match="0 issues read, 1 asked for"):
        github.read_issue_values(C(), ["A"])
    assert len(calls) == github.READ_ATTEMPTS and slept == [github.READ_WAIT] * (github.READ_ATTEMPTS - 1)


def test_issue_without_a_value_its_latest_event_set_is_unread():
    other = {"__typename": "IssueFieldAddedEvent", "createdAt": "t1", "issueField": {"name": "Notes"}}
    gone = {"__typename": "IssueFieldRemovedEvent", "createdAt": "t2", "issueField": {"name": "Start date"}}
    nodes = [
        issue_node("A", events=[SET_START]),  # the read lost the value
        issue_node("B", values=[START], events=[SET_START]),
        issue_node("C", events=[other]),  # not a synced field
        issue_node("D", events=[SET_START, gone]),  # removed after it was set
    ]
    values, unread = github.read_issue_values(Fake(values=nodes), ["A", "B", "C", "D"])
    assert unread == 1 and sorted(values) == ["B", "C", "D"]
    assert values["B"] == {"Start date": ["2026-02-01", "t1"]} and values["D"] == {"Start date": [None, "t2"]}
    assert github.ISSUE_NAMES == {"Start date", "Target date", "Priority"}


def test_missing_page_info_is_an_error():
    p = page([board_item("A")])
    del p["fields"]["pageInfo"]
    with pytest.raises(KeyError):
        github.read_board(Fake(pages=[p]), "o", 1)


SNAP = {
    "projects": {"b2": "P2"},
    "fields": {
        "b2": {
            "Start date": {"id": "FD", "type": "date"},
            "Status": {"id": "FS", "type": "select", "options": {"TBD": "o"}},
        },
        "issue:FormingWorlds": {
            "Target date": {"id": "ID", "type": "date"},
            "Priority": {"id": "IP", "type": "select", "options": {"High": "h"}},
        },
    },
    "items": {"I": {"org": "FormingWorlds", "places": {"b2": {"item_id": "IT"}}}},
}


@pytest.mark.parametrize(
    ("w", "name", "variables"),
    [
        (
            {"place": "b2", "field": "Start date", "value": "2026-01-01"},
            "updateProjectV2ItemFieldValue",
            {"p": "P2", "i": "IT", "f": "FD", "v": {"date": "2026-01-01"}},
        ),
        (
            {"place": "b2", "field": "Status", "value": "TBD"},
            "updateProjectV2ItemFieldValue",
            {"p": "P2", "i": "IT", "f": "FS", "v": {"singleSelectOptionId": "o"}},
        ),
        (
            {"place": "b2", "field": "Status", "value": None},
            "clearProjectV2ItemFieldValue",
            {"p": "P2", "i": "IT", "f": "FS"},
        ),
        (
            {"place": "issue", "field": "Target date", "value": "2026-02-02"},
            "setIssueFieldValue",
            {"i": "I", "v": [{"fieldId": "ID", "dateValue": "2026-02-02"}]},
        ),
        (
            {"place": "issue", "field": "Priority", "value": "High"},
            "setIssueFieldValue",
            {"i": "I", "v": [{"fieldId": "IP", "singleSelectOptionId": "h"}]},
        ),
        ({"place": "issue", "field": "Priority", "value": None}, "deleteIssueFieldValue", {"i": "I", "f": "IP"}),
    ],
)
def test_write(w, name, variables):
    fw, interra = Fake(), Fake()
    clients = {"FormingWorlds": fw, "Interra-Project": interra}
    github.write(clients, SNAP, {"issue": "I", **w})
    sent = fw.sent if w["place"] == "issue" else interra.sent
    assert sent == [(name, variables)]


def test_retry_after_edge_values(slept):
    op = opener_for(http_error(429, {"Retry-After": "0"}), http_error(429, {"Retry-After": "-5"}), {"data": {"ok": 1}})
    assert github.Client("t", op).post("q") == {"ok": 1}
    assert slept == [0, 0]
    assert github._retry_after(None) is None and github._retry_after("soon") is None


def test_rate_limit_text_in_any_case_and_unreadable_bodies(slept):
    op = opener_for(http_error(403, body=b"API Rate Limit exceeded"), {"data": {"ok": 1}})
    assert github.Client("t", op).post("q") == {"ok": 1}

    class Broken(io.BytesIO):
        def read(self, *a):
            raise http.client.IncompleteRead(b"")

    err = urllib.error.HTTPError(github.API, 502, "x", {}, Broken())
    assert github.Client("t", opener_for(err, {"data": {"ok": 2}})).post("q") == {"ok": 2}
    no_body = urllib.error.HTTPError(github.API, 503, "x", {}, None)
    assert github.Client("t", opener_for(no_body, {"data": {"ok": 3}})).post("q") == {"ok": 3}


def test_missing_board_is_named():
    class C:
        def post(self, q, v, allow_not_found=False):
            return {"organization": {"projectV2": None}}

    with pytest.raises(github.BoardNotFound, match="Interra-Project project 9"):
        github.read_board(C(), "Interra-Project", 9)

    class D:
        def post(self, q, v, allow_not_found=False):
            return {"organization": None}

    with pytest.raises(github.BoardNotFound):
        github.read_board(D(), "Nobody", 1)


def test_missing_board_from_the_real_response_shape():
    body = {
        "data": {"organization": {"projectV2": None}},
        "errors": [{"type": "NOT_FOUND", "path": ["organization", "projectV2"], "message": "Could not resolve"}],
    }
    with pytest.raises(github.BoardNotFound, match="Interra-Project project 999"):
        github.read_board(github.Client("t", opener_for(body)), "Interra-Project", 999)
    no_org = {"data": {"organization": None}, "errors": [{"type": "NOT_FOUND", "path": ["organization"]}]}
    with pytest.raises(github.BoardNotFound):
        github.read_board(github.Client("t", opener_for(no_org)), "no-such-org", 1)


def test_board_read_rejects_not_found_below_the_board():
    deep = {
        "data": {"organization": {"projectV2": page([board_item("A")]) | {"id": "P"}}},
        "errors": [{"type": "NOT_FOUND", "path": ["organization", "projectV2", "items", "nodes", 0, "fieldValues"]}],
    }
    with pytest.raises(github.GraphQLError):
        github.read_board(github.Client("t", opener_for(deep)), "o", 1)
