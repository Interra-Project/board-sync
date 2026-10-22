"""Tests of the restore from a stored snapshot."""

import argparse
import copy
import io
import json

import pytest

from board_sync import core, github, restore


def test_synced_names():
    assert core.synced_names("issue") == ["Start date", "Target date", "Priority", "Effort"]
    assert core.synced_names("b7") == ["Start date", "Target date", "Priority", "Workload"]
    assert core.synced_names("b1") == [
        "Start date",
        "Target date",
        "Priority",
        "Workload",
        "Category",
        "Sub-team",
        "Release",
        "Status",
    ]
    assert core.synced_names("b9") == []


def test_plan_sets_changed_and_clears_filled_values(mk):
    old = mk.snap([mk.item(b2={"Start date": ("2026-01-01", "t1")}, issue={"Priority": (None, "t0")})])
    now = copy.deepcopy(old)
    vals = now["items"]["I1"]["places"]
    vals["b2"]["values"]["Start date"] = ["2026-05-05", "t2"]
    vals["b2"]["values"]["Category"] = ["Tech", "t2"]
    vals["issue"]["values"]["Priority"] = ["Urgent", "t2"]
    writes, skipped, left = restore.plan(old, now)
    got = {(w["place"], w["field"]): (w["value"], w["current"]) for w in writes}
    assert got == {
        ("b2", "Start date"): ("2026-01-01", "2026-05-05"),
        ("b2", "Category"): (None, "Tech"),
        ("issue", "Priority"): (None, "Urgent"),
    }
    assert skipped == [] and left == {}
    assert all(w["issue"] == "I1" and w["url"].endswith("/issues/1") for w in writes)


def test_plan_writes_place_values_as_they_were(mk):
    old = mk.snap([mk.item(b1={"Priority": ("Priority: high", "t1"), "Sub-team": ("Co-Leads", "t1")})])
    now = mk.snap([mk.item(b1={"Priority": ("Priority: low", "t2")})])
    writes, _, _ = restore.plan(old, now)
    assert {(w["field"], w["value"]) for w in writes} == {("Priority", "Priority: high"), ("Sub-team", "Co-Leads")}


def test_plan_uses_the_field_set_of_the_issue_org(mk):
    ids = {("issue:FormingWorlds", "Priority"): "fw-prio"}
    old = mk.snap([mk.item("FormingWorlds", issue={"Priority": ("High", "t1")})], ids=ids)
    now = mk.snap([mk.item("FormingWorlds", issue={"Priority": ("Low", "t2")})], ids=ids)
    writes, skipped, _ = restore.plan(old, now)
    assert [(w["place"], w["value"]) for w in writes] == [("issue", "High")] and skipped == []
    now["fields"]["issue:Interra-Project"]["Priority"]["id"] = "other"
    assert len(restore.plan(old, now)[0]) == 1


def test_plan_counts_issues_in_only_one_read(mk):
    old = mk.snap([mk.item(b2={"Start date": ("2026-01-01", "t1")}), mk.item(b7={}), mk.item(b7={})])
    now = mk.snap([mk.item(b2={"Start date": ("2026-01-01", "t1")})])
    assert restore.plan(old, now) == ([], [], {"issue only in the stored read": 2})
    assert restore.plan(now, old) == ([], [], {"issue only in the current read": 2})


def test_plan_counts_places_that_left_or_joined(mk):
    old = mk.snap([mk.item(b1={"Start date": ("2026-01-01", "t1")})])
    now = mk.snap([mk.item(b2={"Start date": ("2026-03-03", "t1")})])
    writes, _, left = restore.plan(old, now)
    assert writes == []
    assert left == {"place left out: b1 only in the stored read": 1, "place left out: b2 only in the current read": 1}


def test_plan_counts_a_field_missing_in_either_read(mk):
    old = mk.snap([mk.item(b2={"Start date": ("2026-01-01", "t1")}, issue={"Effort": ("Low", "t1")})] * 2)
    now = mk.snap([mk.item(b2={"Start date": ("2026-02-02", "t1")}, issue={})] * 2)
    del now["fields"]["b2"]["Start date"]
    del old["fields"]["issue:Interra-Project"]["Effort"]
    writes, _, left = restore.plan(old, now)
    assert writes == []
    assert left == {
        "field left out: b2 / Start date missing": 2,
        "field left out: issue:Interra-Project / Effort missing": 2,
    }


def test_plan_ignores_fields_the_sync_does_not_write(mk):
    old = mk.snap([mk.item(b2={})])
    now = mk.snap([mk.item(b2={"Title": ("x", "t")})])
    now["fields"]["b2"]["Title"] = {"id": "T", "type": "other", "options": {}}
    assert restore.plan(old, now) == ([], [], {})


def test_plan_skips_recreated_fields_and_missing_options(mk):
    old = mk.snap([mk.item(b2={"Start date": ("2026-01-01", "t1"), "Release": ("Interra 2027", "t1")})])
    now = mk.snap([mk.item(b2={})], ids={("b2", "Start date"): "new-id"})
    del now["fields"]["b2"]["Release"]["options"]["Interra 2027"]
    writes, skipped, _ = restore.plan(old, now)
    assert writes == []
    assert {(s[2], s[4]) for s in skipped} == {("Start date", "field was created again"), ("Release", "no such option")}


def test_plan_undoes_a_sync_plan(mk):
    old = mk.snap(
        [
            mk.item(
                b2={
                    "Start date": ("2026-01-01", "t1"),
                    "Workload": ("L", "t1"),
                    "Priority": ("Priority 2: high", "t1"),
                },
                b1={},
                b7={"Start date": ("2025-01-01", "t0")},
                issue={},
            )
        ]
    )
    sync = core.plan(old, {}).writes
    assert len(sync) > 5
    now = copy.deepcopy(old)
    for w in sync:
        now["items"][w["issue"]]["places"][w["place"]]["values"][w["field"]] = [w["value"], "t9"]
    writes, _, _ = restore.plan(old, now)
    assert {(w["place"], w["field"]) for w in writes} == {(w["place"], w["field"]) for w in sync}
    for w in writes:
        now["items"][w["issue"]]["places"][w["place"]]["values"][w["field"]] = [w["value"], "t10"]
    assert restore.plan(old, now) == ([], [], {})


class Recorder:
    """Client that records each GraphQL call as (mutation name, variables)."""

    def __init__(self):
        self.calls = []

    def post(self, query, variables=None, **kw):
        self.calls.append((query.split("{", 2)[1].split("(")[0].strip(), variables))
        return {}


def test_restore_writes_go_through_the_real_write(mk):
    old = mk.snap([mk.item(b1={"Priority": ("Priority: high", "t1")}, b2={}, issue={"Effort": ("Low", "t1")})])
    now = mk.snap(
        [
            mk.item(
                b1={"Priority": ("Priority: low", "t2")},
                b2={"Category": ("Tech", "t2")},
                issue={"Effort": ("High", "t2")},
            )
        ]
    )
    writes, _, _ = restore.plan(old, now)
    client = Recorder()
    for w in writes:
        github.write({"Interra-Project": client}, now, w)
    assert sorted(client.calls, key=lambda c: c[0]) == [
        ("clearProjectV2ItemFieldValue", {"p": "P2", "i": "item-b2", "f": "b2/Category"}),
        (
            "setIssueFieldValue",
            {"i": "I1", "v": [{"fieldId": "issue:Interra-Project/Effort", "singleSelectOptionId": "opt-Low"}]},
        ),
        (
            "updateProjectV2ItemFieldValue",
            {"p": "P1", "i": "item-b1", "f": "b1/Priority", "v": {"singleSelectOptionId": "opt-Priority: high"}},
        ),
    ]


@pytest.fixture
def world(mk, monkeypatch, tmp_path):
    """Store an old snapshot, patch the read to a changed one, and record writes."""
    old = mk.snap([mk.item(b2={"Start date": ("2026-01-01", "t1")}, issue={"Priority": ("High", "t1")})])
    now = copy.deepcopy(old)
    now["items"]["I1"]["places"]["b2"]["values"]["Start date"] = ["2026-09-09", "t2"]
    now["items"]["I1"]["places"]["issue"]["values"]["Priority"] = ["Low", "t2"]
    now["unread_issues"] = {"FormingWorlds": 2}
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(old))
    monkeypatch.setattr(github, "read_all", lambda clients: now)
    done = []
    monkeypatch.setattr(github, "write", lambda clients, snap, w: done.append(w))
    monkeypatch.setattr(restore.time, "sleep", lambda s: done.append(("sleep", s)))
    return path, done, restore.plan(old, now)[0]


def ns(path, tmp_path, **kw):
    base = {"snapshot": str(path), "apply": False, "expect": None, "pause": 0.0, "report": str(tmp_path / "r.txt")}
    return argparse.Namespace(**{**base, **kw})


def test_plan_id_depends_on_the_writes_not_their_order(world):
    *_, writes = world
    assert restore.plan_id(writes) == restore.plan_id(writes[::-1])
    changed = [dict(writes[0], value="2026-01-02")] + writes[1:]
    assert restore.plan_id(changed) != restore.plan_id(writes)
    assert len(restore.plan_id(writes)) == 12


def test_dry_run_reports_and_writes_nothing(world, tmp_path):
    path, done, writes = world
    out = io.StringIO()
    assert restore.run(ns(path, tmp_path), {}, out) == 0
    assert done == []
    lines = out.getvalue().splitlines()
    assert lines[0] == f"restore writes: 2  skipped: 0  plan id: {restore.plan_id(writes)}"
    assert "      1  Interra Collaboration / Start date" in lines
    assert "warning: 2 FormingWorlds issues could not be read for their issue fields" in lines
    assert (
        "restore  https://github.com/Interra-Project/repo/issues/1  issue field / Priority: High  (now: Low)" in lines
    )
    assert (tmp_path / "r.txt").read_text() == out.getvalue()


def test_dry_run_lists_skipped_values_and_left_out_places(world, mk, monkeypatch, tmp_path):
    path, *_ = world
    now = mk.snap([mk.item(b1={}, issue={"Priority": ("Low", "t2")})], ids={("issue:Interra-Project", "Priority"): "x"})
    monkeypatch.setattr(github, "read_all", lambda clients: now)
    out = io.StringIO()
    assert restore.run(ns(path, tmp_path), {}, out) == 0
    text = out.getvalue()
    assert "restore writes: 0  skipped: 1" in text
    assert (
        "skipped https://github.com/Interra-Project/repo/issues/1 issue / Priority: 'High' (field was created again)"
        in text
    )
    assert "note: 1 place left out: b2 only in the stored read" in text
    assert "note: 1 place left out: b1 only in the current read" in text


def test_apply_needs_the_plan_id(world, tmp_path):
    path, done, writes = world
    for expect in (None, "000000000000"):
        out = io.StringIO()
        assert restore.run(ns(path, tmp_path, apply=True, expect=expect), {}, out) == 3
        stop = f"stop: --expect {expect} differs from the plan id {restore.plan_id(writes)}"
        assert out.getvalue().splitlines()[1] == stop
        assert (tmp_path / "r.txt").read_text().splitlines()[1] == stop
    assert done == []


def test_apply_writes_and_reports(world, tmp_path):
    path, done, writes = world
    out = io.StringIO()
    args = ns(path, tmp_path, apply=True, expect=restore.plan_id(writes), pause=0.5)
    assert restore.run(args, {}, out) == 0
    assert done == [writes[0], ("sleep", 0.5), writes[1]]
    assert out.getvalue().splitlines()[1] == "written: 2  failed: 0  not tried: 0"
    assert (tmp_path / "r.txt").read_text() == out.getvalue()


def test_report_holds_the_plan_before_the_first_write(world, monkeypatch, tmp_path):
    path, _, writes = world
    seen = []
    monkeypatch.setattr(github, "write", lambda c, s, w: seen.append((tmp_path / "r.txt").read_text()))
    assert restore.run(ns(path, tmp_path, apply=True, expect=restore.plan_id(writes)), {}, io.StringIO()) == 0
    assert seen[0].startswith("restore writes: 2") and "written:" not in seen[0]
    assert "issue field / Priority: High  (now: Low)" in seen[0]


def test_partial_failure_is_reported(world, monkeypatch, tmp_path):
    path, _, writes = world
    calls = []

    def first_fails(clients, snap, w):
        calls.append(w)
        if len(calls) == 1:
            raise github.GraphQLError("no access")

    monkeypatch.setattr(github, "write", first_fails)
    out = io.StringIO()
    assert restore.run(ns(path, tmp_path, apply=True, expect=restore.plan_id(writes)), {}, out) == 1
    text = out.getvalue()
    assert "written: 1  failed: 1  not tried: 0" in text
    assert "failed https://github.com/Interra-Project/repo/issues/1  issue field / Priority: High: no access" in text


def many(mk, monkeypatch, tmp_path, n):
    """Store n issues whose b2 start date changed, and return the snapshot path and plan id."""
    old = mk.snap([mk.item(b2={"Start date": (f"2026-01-{d + 1:02d}", "t1")}) for d in range(n)])
    now = copy.deepcopy(old)
    for it in now["items"].values():
        it["places"]["b2"]["values"]["Start date"] = ["2027-01-01", "t2"]
    path = tmp_path / "s.json"
    path.write_text(json.dumps(old))
    monkeypatch.setattr(github, "read_all", lambda clients: now)
    return path, restore.plan_id(restore.plan(old, now)[0])


def test_apply_stops_after_failures_in_a_row(mk, monkeypatch, tmp_path):
    n = restore.STOP_AFTER_FAILURES + 2
    path, pid = many(mk, monkeypatch, tmp_path, n)
    tried = []

    def refuse(clients, snap, w):
        tried.append(w)
        raise github.GraphQLError("refused")

    monkeypatch.setattr(github, "write", refuse)
    out = io.StringIO()
    assert restore.run(ns(path, tmp_path, apply=True, expect=pid), {}, out) == 1
    assert len(tried) == restore.STOP_AFTER_FAILURES
    text = out.getvalue()
    assert f"written: 0  failed: {restore.STOP_AFTER_FAILURES}  not tried: 2" in text
    assert f"stopped after {restore.STOP_AFTER_FAILURES} failed writes in a row" in text


def test_a_success_resets_the_failures_in_a_row(mk, monkeypatch, tmp_path):
    n = 2 * restore.STOP_AFTER_FAILURES
    path, pid = many(mk, monkeypatch, tmp_path, n)
    tried = []

    def every_other(clients, snap, w):
        tried.append(w)
        if len(tried) % 2:
            raise github.GraphQLError("refused")

    monkeypatch.setattr(github, "write", every_other)
    out = io.StringIO()
    assert restore.run(ns(path, tmp_path, apply=True, expect=pid), {}, out) == 1
    assert len(tried) == n
    assert f"written: {n // 2}  failed: {n // 2}  not tried: 0" in out.getvalue()


def test_report_is_saved_when_the_run_is_cut_short(world, monkeypatch, tmp_path):
    path, _, writes = world

    def stop(clients, snap, w):
        raise KeyboardInterrupt

    monkeypatch.setattr(github, "write", stop)
    with pytest.raises(KeyboardInterrupt):
        restore.run(ns(path, tmp_path, apply=True, expect=restore.plan_id(writes)), {}, io.StringIO())
    assert (tmp_path / "r.txt").read_text().splitlines()[1] == "written: 0  failed: 0  not tried: 2"


@pytest.mark.parametrize("apply", [False, True])
def test_bad_report_path_exits_2_before_any_write(world, tmp_path, apply):
    path, done, writes = world
    out = io.StringIO()
    args = ns(path, tmp_path, apply=apply, expect=restore.plan_id(writes), report=str(tmp_path / "no" / "r.txt"))
    assert restore.run(args, {}, out) == 2
    assert done == []
    assert "report not written: " in out.getvalue()
    assert ("restore writes: 2" in out.getvalue()) is not apply


@pytest.mark.parametrize("content", [None, "not json", '{"items": {"I1": {}}}'])
def test_setup_errors_exit_2_and_are_reported(world, tmp_path, content):
    _, done, _ = world
    bad = tmp_path / "bad.json"
    if content is not None:
        bad.write_text(content)
    out = io.StringIO()
    assert restore.run(ns(bad, tmp_path), {}, out) == 2
    assert out.getvalue().startswith("restore failed: ") and done == []
    assert (tmp_path / "r.txt").read_text() == out.getvalue()


def test_read_failure_exits_2(world, monkeypatch, tmp_path):
    path, *_ = world

    def broken(clients):
        raise github.TruncatedRead("fields")

    monkeypatch.setattr(github, "read_all", broken)
    out = io.StringIO()
    assert restore.run(ns(path, tmp_path, report=None), {}, out) == 2
    assert "restore failed: TruncatedRead" in out.getvalue()


def test_main_parses_arguments_and_restores_the_signal_handler(world, monkeypatch, capsys):
    path, *_ = world
    seen = {}

    def fake_run(args, clients):
        seen["args"], seen["clients"] = args, clients
        seen["handler"] = restore.signal.getsignal(restore.signal.SIGTERM)
        return 0

    monkeypatch.setattr(restore, "run", fake_run)
    before = restore.signal.getsignal(restore.signal.SIGTERM)
    argv = [str(path), "--apply", "--expect", "abc", "--report", "r.txt"]
    assert restore.main(argv, {"GH_TOKEN": "g"}) == 0
    assert seen["args"].apply and seen["args"].expect == "abc" and seen["args"].pause == 1.0
    assert set(seen["clients"]) == {"Interra-Project", "FormingWorlds"}
    assert seen["handler"] is restore._terminate and restore.signal.getsignal(restore.signal.SIGTERM) == before
    for bad, msg in (
        (["--pause", "-1"], "--pause must not be negative"),
        (["--apply", "--expect", "abc"], "--apply needs --report"),
    ):
        with pytest.raises(SystemExit):
            restore.main([str(path), *bad], {"GH_TOKEN": "g"})
        assert msg in capsys.readouterr().err
