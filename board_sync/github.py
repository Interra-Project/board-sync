"""Read and write the GitHub places: project boards and org issue fields."""

import http.client
import json
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

from . import config

API = "https://api.github.com/graphql"

_FIELD_NAME = (
    "... on IssueFieldDate { name } ... on IssueFieldSingleSelect { name } "
    "... on IssueFieldText { name } ... on IssueFieldNumber { name } "
    "... on IssueFieldMultiSelect { name }"
)

BOARD_QUERY = """
query($owner: String!, $number: Int!, $after: String) {
  organization(login: $owner) { projectV2(number: $number) { id
    fields(first: 100) { pageInfo { hasNextPage } nodes {
      ... on ProjectV2FieldCommon { id name dataType }
      ... on ProjectV2SingleSelectField { options { id name } } } }
    items(first: 100, after: $after) { totalCount pageInfo { hasNextPage endCursor } nodes { id
      content { ... on Issue { id url repository { owner { login } } } }
      fieldValues(first: 100) { totalCount pageInfo { hasNextPage } nodes {
        ... on ProjectV2ItemFieldDateValue { date updatedAt field { ... on ProjectV2FieldCommon { name } } }
        ... on ProjectV2ItemFieldSingleSelectValue { name updatedAt field { ... on ProjectV2FieldCommon { name } } }
      } } } } } }
}
"""

ISSUE_FIELDS_QUERY = """
query($org: String!) { organization(login: $org) { issueFields(first: 100) { pageInfo { hasNextPage } nodes {
  ... on IssueFieldDate { id name }
  ... on IssueFieldSingleSelect { id name options { id name } } } } } }
"""

# Field events read per issue: the most recent ones, up to this number.
TIMELINE_LAST = 100

ISSUE_VALUES_QUERY = (
    (
        """
query($ids: [ID!]!) { nodes(ids: $ids) { ... on Issue { id
  issueFieldValues(first: 50) { totalCount pageInfo { hasNextPage } nodes {
    ... on IssueFieldDateValue { value field { ... on IssueFieldDate { name } } }
    ... on IssueFieldSingleSelectValue { name field { ... on IssueFieldSingleSelect { name } } } } }
  timelineItems(last: __LAST__, itemTypes: [ISSUE_FIELD_ADDED_EVENT, ISSUE_FIELD_CHANGED_EVENT,
                                            ISSUE_FIELD_REMOVED_EVENT]) { totalCount nodes {
    __typename
    ... on IssueFieldAddedEvent { createdAt issueField { __FIELD_NAME__ } }
    ... on IssueFieldChangedEvent { createdAt issueField { __FIELD_NAME__ } }
    ... on IssueFieldRemovedEvent { createdAt issueField { __FIELD_NAME__ } } } } } }
}
"""
    )
    .replace("__FIELD_NAME__", _FIELD_NAME)
    .replace("__LAST__", str(TIMELINE_LAST))
)


class GraphQLError(RuntimeError):
    """The GraphQL API returned errors."""


# Error paths of a missing board or org; any other NOT_FOUND in a board read stops the read.
BOARD_GONE = (("organization",), ("organization", "projectV2"))


class BoardNotFound(RuntimeError):
    """A configured board does not exist or the token cannot see it."""


class TruncatedRead(RuntimeError):
    """A list in a response was longer than the request asked for."""


class ShortRead(RuntimeError):
    """A read gave fewer entries than the API reports, or none where there must be some."""


class ShortBoardRead(ShortRead):
    """A board read gave no items, a number of items other than the board total, or an item without values."""


class ShortIssueRead(ShortRead):
    """A read of issue field values gave fewer issues than asked for, or a number of values or events other than the total."""


# Attempts of one read, and seconds between them, when a read comes back short.
READ_ATTEMPTS = 6
READ_WAIT = 10

# Issue field names that the sync reads, for the check of values against their latest events.
ISSUE_NAMES = frozenset(spec["places"]["issue"] for spec in config.FIELDS.values() if "issue" in spec["places"])


def _check_page(conn, what):
    """Raise ``TruncatedRead`` if a connection has more nodes than were read."""
    if conn["pageInfo"]["hasNextPage"]:
        raise TruncatedRead(what)


def _count(nodes):
    """Number of entries in a node list that are not null; a null list counts 0."""
    return sum(n is not None for n in nodes or ())


def _read_again(read):
    """Call ``read`` until it does not raise ``ShortRead``, at most ``READ_ATTEMPTS`` times in all."""
    for _ in range(READ_ATTEMPTS - 1):
        try:
            return read()
        except ShortRead:
            time.sleep(READ_WAIT)
    return read()


class Client:
    """Minimal GraphQL client for one token.

    Parameters
    ----------
    token : str
        GitHub token (installation token or personal token).
    opener : callable, optional
        ``urllib.request.urlopen`` or a test replacement.
    timeout : float
        Seconds to wait for one response.
    """

    def __init__(self, token, opener=urllib.request.urlopen, timeout=60):
        self.token = token
        self.opener = opener
        self.timeout = timeout

    def post(self, query, variables=None, retries=2, allow_not_found=False):
        """Run one GraphQL request and return its ``data``.

        Parameters
        ----------
        query : str
            GraphQL document.
        variables : dict, optional
            Query variables.
        retries : int
            Retries after an HTTP 502, 503 or 504, a network error, or a rate
            limit (HTTP 403 or 429, or a ``RATE_LIMITED`` error).
        allow_not_found : bool or tuple
            Return the partial ``data`` when every error is of type
            ``NOT_FOUND`` (for example a deleted issue in a ``nodes`` list).
            A tuple of paths further limits this to errors at those paths.

        Returns
        -------
        dict
            The ``data`` member of the response.

        Raises
        ------
        GraphQLError
            If the response holds errors.
        urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException
            If the request still fails after the retries.
        """
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            API,
            data=body,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                out = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                text = exc.read(65536).decode("utf-8", "replace") if exc.fp else ""
            except (OSError, http.client.HTTPException):
                text = ""
            exc.close()
            if retries > 0 and exc.code in (502, 503, 504):
                time.sleep(5)
                return self.post(query, variables, retries - 1, allow_not_found)
            wait = _retry_after((exc.headers or {}).get("Retry-After"))
            limited = exc.code == 429 or (exc.code == 403 and (wait is not None or "rate limit" in text.lower()))
            if retries > 0 and limited:
                time.sleep(60 if wait is None else wait)
                return self.post(query, variables, retries - 1, allow_not_found)
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
            if retries > 0:
                time.sleep(5)
                return self.post(query, variables, retries - 1, allow_not_found)
            raise
        errors = out.get("errors")
        if errors and retries > 0 and any(e.get("type") == "RATE_LIMITED" for e in errors):
            time.sleep(60)
            return self.post(query, variables, retries - 1, allow_not_found)
        if errors:
            paths = allow_not_found if isinstance(allow_not_found, tuple) else None
            found = all(
                e.get("type") == "NOT_FOUND" and (paths is None or tuple(e.get("path") or ()) in paths) for e in errors
            )
            if allow_not_found and out.get("data") is not None and found:
                return out["data"]
            raise GraphQLError(json.dumps(errors)[:2000])
        return out["data"]


def _retry_after(value):
    """Return the ``Retry-After`` wait in seconds (0 to 120), or ``None`` if absent or not a number."""
    try:
        return max(0, min(int(value), 120))
    except (TypeError, ValueError):
        return None


def _field_type(data_type):
    """Map a GraphQL field data type to ``date``, ``select`` or ``other``."""
    return {"DATE": "date", "SINGLE_SELECT": "select"}.get(data_type, "other")


def read_board(client, owner, number):
    """Read the fields and the issue items of one project board.

    A read that finds no items, a number of items other than the board's
    ``totalCount``, a ``totalCount`` that changes between pages, or an issue
    item whose values are none or other than their ``totalCount``, is short:
    the board is read up to ``READ_ATTEMPTS`` times in all, since the API can
    return an empty page with no error. Every item holds at least its title.

    Parameters
    ----------
    client : Client
        Client with read access to the board.
    owner : str
        Org login that owns the board.
    number : int
        Board number.

    Returns
    -------
    tuple
        Project id, field definitions by name, items as a list of
        ``(issue_id, url, org, item_id, values)``, and the number
        of items skipped because they are not issues or cannot be read.

    Raises
    ------
    BoardNotFound
        If the board or its org does not exist or the token cannot see it.
    TruncatedRead
        If a board has more fields, or an item more values, than one page.
    ShortBoardRead
        If every attempt is short.
    """
    return _read_again(lambda: _read_board_once(client, owner, number))


def _read_board_once(client, owner, number):
    """Read one board once; see ``read_board``."""
    after, items, fields, pid, skipped = None, [], None, None, 0
    total, seen = None, 0
    while True:
        # A missing board or org comes back as null data with a NOT_FOUND error.
        data = client.post(BOARD_QUERY, {"owner": owner, "number": number, "after": after}, allow_not_found=BOARD_GONE)
        proj = (data.get("organization") or {}).get("projectV2")
        if proj is None:
            raise BoardNotFound(f"{owner} project {number}")
        pid = proj["id"]
        if fields is None:
            _check_page(proj["fields"], f"fields of board {number}")
            fields = {
                f["name"]: {
                    "id": f["id"],
                    "type": _field_type(f["dataType"]),
                    "options": {o["name"]: o["id"] for o in f.get("options", [])},
                }
                for f in proj["fields"]["nodes"]
                if f
            }
        if total is None:
            total = proj["items"]["totalCount"]
        elif proj["items"]["totalCount"] != total:
            raise ShortBoardRead(f"board {number}: total {proj['items']['totalCount']} on a later page, {total} first")
        seen += len(proj["items"]["nodes"])
        for node in proj["items"]["nodes"]:
            content = (node or {}).get("content") or {}
            if "id" not in content:
                skipped += 1  # draft issue, pull request, or an issue the token cannot read
                continue
            fv = node.get("fieldValues") or {"totalCount": 0, "pageInfo": {"hasNextPage": False}, "nodes": []}
            _check_page(fv, f"values of an item on board {number}")
            got = _count(fv["nodes"])
            if fv["totalCount"] == 0 or got != fv["totalCount"]:
                raise ShortBoardRead(f"board {number}: an item has {got} values, total {fv['totalCount']}")
            values = {}
            for v in fv["nodes"]:
                if v and v.get("field"):
                    values[v["field"]["name"]] = [v.get("date", v.get("name")), v.get("updatedAt")]
            org = content["repository"]["owner"]["login"]
            items.append((content["id"], content["url"], org, node["id"], values))
        page = proj["items"]["pageInfo"]
        if not page["hasNextPage"]:
            if total == 0 or seen != total:
                raise ShortBoardRead(f"board {number}: {seen} items read, total {total}")
            return pid, fields, items, skipped
        after = page["endCursor"]


def read_issue_fields(client, org):
    """Read the org issue field definitions.

    Parameters
    ----------
    client : Client
        Client with access to the org.
    org : str
        Org login.

    Returns
    -------
    dict
        Field name to definition.
    """
    conn = client.post(ISSUE_FIELDS_QUERY, {"org": org})["organization"]["issueFields"]
    _check_page(conn, f"issue fields of {org}")
    return {
        n["name"]: {
            "id": n["id"],
            "type": "select" if "options" in n else "date",
            "options": {o["name"]: o["id"] for o in n.get("options", [])},
        }
        for n in conn["nodes"]
        if n
    }


def read_issue_values(client, ids, batch=25):
    """Read org issue field values and their change times.

    A field whose latest event is a removal reads as ``[None, removed_at]``,
    so a clear has its own time. A batch that returns fewer issues than
    asked for, an issue with a number of values other than its
    ``totalCount``, or a timeline with a number of events other than its
    ``totalCount`` (at most ``TIMELINE_LAST``) or with no ``totalCount``,
    is read again, up to ``READ_ATTEMPTS`` times in all. An issue that lacks
    the value of a synced field whose latest event set it counts as unread,
    and so does one with a synced value but no event for it while the
    timeline was read in full; an unread issue is not read again in the
    same run.

    Parameters
    ----------
    client : Client
        Client of the org that owns the issues.
    ids : list of str
        Issue node ids.
    batch : int
        Issues per request.

    Returns
    -------
    tuple
        Issue id to ``{field name: [value, changed_at]}``, and the number of
        issues that could not be read.

    Raises
    ------
    ShortIssueRead
        If every attempt of a batch is short.
    """
    out, unread = {}, 0
    for i in range(0, len(ids), batch):
        part = ids[i : i + batch]
        values, n = _read_again(lambda part=part: _read_issue_batch(client, part))
        out.update(values)
        unread += n
    return out, unread


def _read_issue_batch(client, ids):
    """Read the issue field values of one batch once; see ``read_issue_values``."""
    out, unread = {}, 0
    data = client.post(ISSUE_VALUES_QUERY, {"ids": ids}, allow_not_found=True)
    if len(data["nodes"]) != len(ids):
        raise ShortIssueRead(f"{len(data['nodes'])} issues read, {len(ids)} asked for")
    for node in data["nodes"]:
        if not node:
            unread += 1
            continue
        conn = node.get("issueFieldValues")
        if conn is None or conn.get("nodes") is None:
            raise ShortIssueRead("an issue has no field value list")
        _check_page(conn, "issue field values")
        if _count(conn["nodes"]) != conn["totalCount"]:
            raise ShortIssueRead(f"an issue has {_count(conn['nodes'])} field values, total {conn['totalCount']}")
        timeline = node.get("timelineItems")
        if timeline is None or timeline.get("nodes") is None:
            raise ShortIssueRead("an issue has no timeline list")
        total = timeline.get("totalCount")
        if not isinstance(total, int) or _count(timeline["nodes"]) != min(total, TIMELINE_LAST):
            raise ShortIssueRead(f"an issue has {_count(timeline['nodes'])} events, total {total}")
        times, removed = {}, set()
        for ev in timeline["nodes"]:
            name = ((ev or {}).get("issueField") or {}).get("name")
            if name and ev["createdAt"] >= times.get(name, ""):
                times[name] = ev["createdAt"]
                if ev.get("__typename") == "IssueFieldRemovedEvent":
                    removed.add(name)
                else:
                    removed.discard(name)
        values = {name: [None, times[name]] for name in removed}
        for v in conn["nodes"]:
            if v and v.get("field"):
                name = v["field"]["name"]
                values[name] = [v.get("value", v.get("name")), times.get(name)]
        if any(name not in values for name in (ISSUE_NAMES & times.keys()) - removed):
            unread += 1  # a value that its latest event set is absent from the read
            continue
        if total <= TIMELINE_LAST and any(values[n][1] is None for n in ISSUE_NAMES & values.keys()):
            unread += 1  # a synced value with no event although the timeline was read in full
            continue
        out[node["id"]] = values
    return out, unread


def read_all(clients):
    """Read every place into one snapshot.

    Parameters
    ----------
    clients : dict
        Org login to ``Client``.

    Returns
    -------
    dict
        Snapshot with keys ``fields``, ``projects``, ``items``,
        ``skipped_items`` (per board) and ``unread_issues`` (per org).
    """
    snap = {
        "fields": {},
        "projects": {},
        "items": {},
        "skipped_items": {},
        "unread_issues": {},
        "read_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    for key, board in config.BOARDS.items():
        pid, fields, items, skipped = read_board(clients[board["owner"]], board["owner"], board["number"])
        snap["projects"][key] = pid
        snap["fields"][key] = fields
        snap["skipped_items"][key] = skipped
        for iid, url, org, item_id, values in items:
            entry = snap["items"].setdefault(iid, {"url": url, "org": org, "places": {}})
            entry["places"][key] = {"item_id": item_id, "values": values}
    for org in config.ISSUE_ORGS:
        snap["fields"][f"issue:{org}"] = read_issue_fields(clients[org], org)
        ids = sorted(i for i, e in snap["items"].items() if e["org"] == org)
        values_by_issue, snap["unread_issues"][org] = read_issue_values(clients[org], ids)
        for iid, values in values_by_issue.items():
            snap["items"][iid]["places"]["issue"] = {"values": values}
    return snap


def write(clients, snap, w):
    """Apply one planned write.

    Parameters
    ----------
    clients : dict
        Org login to ``Client``.
    snap : dict
        Snapshot the plan was made from.
    w : dict
        One entry of ``Plan.writes``.
    """
    item = snap["items"][w["issue"]]
    if w["place"] == "issue":
        org = item["org"]
        fdef = snap["fields"][f"issue:{org}"][w["field"]]
        if w["value"] is None:
            q = "mutation($i: ID!, $f: ID!) { deleteIssueFieldValue(input: {issueId: $i, fieldId: $f}) { clientMutationId } }"
            clients[org].post(q, {"i": w["issue"], "f": fdef["id"]})
            return
        val = {"fieldId": fdef["id"]}
        if fdef["type"] == "date":
            val["dateValue"] = w["value"]
        else:
            val["singleSelectOptionId"] = fdef["options"][w["value"]]
        q = "mutation($i: ID!, $v: [IssueFieldCreateOrUpdateInput!]!) { setIssueFieldValue(input: {issueId: $i, issueFields: $v}) { clientMutationId } }"
        clients[org].post(q, {"i": w["issue"], "v": [val]})
        return
    board = config.BOARDS[w["place"]]
    fdef = snap["fields"][w["place"]][w["field"]]
    args = {
        "p": snap["projects"][w["place"]],
        "i": item["places"][w["place"]]["item_id"],
        "f": fdef["id"],
    }
    if w["value"] is None:
        q = "mutation($p: ID!, $i: ID!, $f: ID!) { clearProjectV2ItemFieldValue(input: {projectId: $p, itemId: $i, fieldId: $f}) { clientMutationId } }"
        clients[board["owner"]].post(q, args)
        return
    if fdef["type"] == "date":
        args["v"] = {"date": w["value"]}
    else:
        args["v"] = {"singleSelectOptionId": fdef["options"][w["value"]]}
    q = "mutation($p: ID!, $i: ID!, $f: ID!, $v: ProjectV2FieldValue!) { updateProjectV2ItemFieldValue(input: {projectId: $p, itemId: $i, fieldId: $f, value: $v}) { clientMutationId } }"
    clients[board["owner"]].post(q, args)
