"""Shared builders for snapshots in the tests."""

import copy

import pytest

DATE = {"type": "date", "options": {}}


def select(*names):
    """Return a select field definition with the given option names."""
    return {"type": "select", "options": {n: f"opt-{n}" for n in names}}


def fields(ids=None):
    """Return field definitions for all places, with optional id overrides."""
    ids = ids or {}
    status = select("In Progress", "Next Up", "TBD", "Done", "Import", "Starter Projects")
    board = {
        "Start date": dict(DATE),
        "Target date": dict(DATE),
        "Priority": select("Priority 1: critical", "Priority 2: high", "Priority 3: standard", "Priority 4: low"),
        "Workload": select("XL", "L", "M", "S", "XS"),
        "Category": select("Science", "Tech"),
        "Sub-team": select("PIs", "Co-leads"),
        "Release": select("Interra 2027"),
        "Status": status,
    }
    b1 = dict(board)
    b1["Priority"] = select("Priority: critical", "Priority: high", "Priority: normal", "Priority: low")
    b1["Sub-team"] = select("PIs", "Co-Leads")
    b1["Status"] = select("In Progress", "Next Up", "TBD", "Done")
    b7 = {k: board[k] for k in ("Start date", "Target date", "Priority", "Workload")}
    issue = {
        "Start date": dict(DATE),
        "Target date": dict(DATE),
        "Priority": select("Urgent", "High", "Medium", "Low"),
        "Effort": select("High", "Medium", "Low"),
    }
    out = {"b1": b1, "b2": board, "b7": b7, "issue:Interra-Project": issue, "issue:FormingWorlds": issue}
    out = {
        k: {n: {**copy.deepcopy(f), "id": ids.get((k, n), f"{k}/{n}")} for n, f in v.items()} for k, v in out.items()
    }
    return out


def item(org="Interra-Project", **places):
    """Return one snapshot item; each keyword is a place with ``{field: (value, at)}``."""
    return {
        "url": f"https://github.com/{org}/repo/issues/1",
        "org": org,
        "places": {
            p: ({"values": {k: list(v) for k, v in vals.items()}} | ({} if p == "issue" else {"item_id": f"item-{p}"}))
            for p, vals in places.items()
        },
    }


def snap(items, ids=None):
    """Return a snapshot with the given items keyed ``I1``, ``I2``, ..."""
    return {
        "fields": fields(ids),
        "projects": {"b1": "P1", "b2": "P2", "b7": "P7"},
        "items": {f"I{n + 1}": it for n, it in enumerate(items)},
    }


@pytest.fixture
def mk():
    """Expose the builders to the tests."""
    return type("Builders", (), {"item": staticmethod(item), "snap": staticmethod(snap)})
