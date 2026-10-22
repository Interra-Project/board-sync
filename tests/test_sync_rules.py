"""Tests of the sync rules across runs: bulk approval, parking, returning places and time limits."""

import io
import json

import pytest

import board_sync.__main__ as cli
from board_sync import config, core, github
from tests.test_cli import args


def writes(result):
    return {(w["place"], w["field"]): w["value"] for w in result.writes}


def test_bulk_drain_continues_on_scheduled_runs(mk, monkeypatch, tmp_path):
    s = mk.snap(
        [
            mk.item(
                b1={},
                b2={
                    "Category": ("Tech", "t1"),
                    "Start date": ("2026-01-01", "t1"),
                    "Target date": ("2026-02-01", "t1"),
                },
            )
        ]
    )
    monkeypatch.setattr(github, "read_all", lambda clients: s)

    done = []

    def fake_write(clients, snap, w):
        done.append(w["field"])
        snap["items"][w["issue"]]["places"][w["place"]]["values"][w["field"]] = [w["value"], "t9"]

    monkeypatch.setattr(github, "write", fake_write)
    assert cli.run(args(tmp_path, apply=True, max_changes=1, limit=1, allow_bulk=True), {}, io.StringIO()) == 0
    # Scheduled runs (no allow_bulk) go on with the approved remainder until it is written.
    assert cli.run(args(tmp_path, apply=True, max_changes=1, limit=1), {}, io.StringIO()) == 0
    assert cli.run(args(tmp_path, apply=True, max_changes=1, limit=1), {}, io.StringIO()) == 0
    assert sorted(done) == ["Category", "Start date", "Target date"]


def test_readded_item_receives_and_does_not_clear(mk):
    a = mk.item(b2={"Priority": ("Priority 2: high", "t1")}, b7={"Priority": ("Priority 2: high", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    gone = mk.item(b2={"Priority": ("Priority 2: high", "t1")})
    state = core.plan(mk.snap([gone]), state).state
    back = mk.item(b2={"Priority": ("Priority 2: high", "t1")}, b7={})
    assert writes(core.plan(mk.snap([back]), state)) == {("b7", "Priority"): "Priority 2: high"}


def test_field_missing_for_a_run_keeps_its_id(mk):
    a = mk.item(b1={"Category": ("Tech", "t1")}, b2={"Category": ("Tech", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    s = mk.snap([mk.item(b1={"Category": ("Tech", "t1")}, b2={})])
    del s["fields"]["b2"]["Category"]
    state = core.plan(s, state).state
    s = mk.snap([mk.item(b1={"Category": ("Tech", "t1")}, b2={})], ids={("b2", "Category"): "NEW"})
    assert writes(core.plan(s, state)) == {("b2", "Category"): "Tech"}


def test_issue_field_clear_uses_its_removal_time(mk):
    a = mk.item(b2={"Target date": ("2026-05-01", "2026-01-01")}, issue={"Target date": ("2026-05-01", None)})
    state = core.plan(mk.snap([a]), {}).state
    b = mk.item(b2={"Target date": ("2026-06-01", "2026-02-01")}, issue={"Target date": (None, "2026-03-01")})
    assert writes(core.plan(mk.snap([b]), state)) == {("b2", "Target date"): None}


def test_board_clear_loses_to_a_value_set_elsewhere(mk):
    a = mk.item(b1={"Category": ("Tech", "2026-01-01")}, b2={"Category": ("Tech", "2026-01-01")})
    state = core.plan(mk.snap([a]), {}).state
    b = mk.item(b1={"Category": ("Science", "2026-02-01")}, b2={})
    r = core.plan(mk.snap([b]), state)
    assert writes(r) == {("b2", "Category"): "Science"} and r.conflicts
    alone = mk.item(b1={"Category": ("Tech", "2026-01-01")}, b2={})
    assert writes(core.plan(mk.snap([alone]), state)) == {("b1", "Category"): None}


def test_unreadable_place_returning_with_its_old_value_does_not_undo_a_newer_value(mk):
    both = mk.item(b2={"Start date": ("2026-05-01", "t1")}, issue={"Start date": ("2026-05-01", "t1")}, b1={})
    s1 = mk.snap([both])
    state = core.plan(s1, {}).state
    apply_to(s1, core.plan(s1, {}))
    state = core.plan(s1, state).state
    # Run 2: the issue is unreadable, and board 1 gets a new date.
    s2 = mk.snap([mk.item(b2={"Start date": ("2026-05-01", "t1")}, b1={"Start date": ("2026-06-01", "t2")})])
    r2 = core.plan(s2, state)
    assert writes(r2) == {("b2", "Start date"): "2026-06-01"}
    apply_to(s2, r2)
    # Run 3: the issue returns with its old date; the newer board date must win.
    s3 = mk.snap(
        [
            mk.item(
                b2={"Start date": ("2026-06-01", "t3")},
                b1={"Start date": ("2026-06-01", "t2")},
                issue={"Start date": ("2026-05-01", "t1")},
            )
        ]
    )
    assert writes(core.plan(s3, r2.state)) == {("issue", "Start date"): "2026-06-01"}


def test_issue_absent_from_every_board_keeps_its_records(mk):
    a = mk.item(b1={"Category": ("Tech", "t1")}, b2={"Category": ("Tech", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    state = core.plan(mk.snap([]), state).state
    assert state["items"]["I1"]["category"]["gone"] == ["b1", "b2"]
    back = mk.item(b1={"Category": ("Tech", "t1")}, b2={})
    assert writes(core.plan(mk.snap([back]), state)) == {("b2", "Category"): "Tech"}


def test_field_on_no_place_keeps_its_record(mk):
    a = mk.item(b1={"Status": ("In Progress", "t1")}, b2={"Status": ("In Progress", "t1")}, b7={})
    state = core.plan(mk.snap([a]), {}).state
    s = mk.snap([mk.item(b7={})])  # only on board 7, which has no Status
    state = core.plan(s, state).state
    assert state["items"]["I1"]["status"]["c"] == "In Progress"
    back = mk.item(b1={}, b2={"Status": ("In Progress", "t1")}, b7={})
    assert writes(core.plan(mk.snap([back]), state)) == {("b1", "Status"): "In Progress"}


def test_missing_fields_are_listed(mk):
    s = mk.snap([])
    del s["fields"]["b7"]["Priority"]
    del s["fields"]["issue:FormingWorlds"]["Target date"]
    assert core.missing_fields(s) == [("b7", "Priority"), ("issue:FormingWorlds", "Target date")]


def test_reset_min_boundary(mk):
    from board_sync import config

    n = config.RESET_MIN
    for count, clears in ((n - 1, True), (n, False)):
        full = [
            mk.item(b1={"Release": ("Interra 2027", "t")}, b2={"Release": ("Interra 2027", "t")}) for _ in range(count)
        ]
        state = core.plan(mk.snap(full), {}).state
        empty = [mk.item(b1={"Release": ("Interra 2027", "t")}, b2={}) for _ in range(count)]
        values = {w["value"] for w in core.plan(mk.snap(empty), state).writes}
        assert values == ({None} if clears else {"Interra 2027"})


def apply_to(snap, result):
    """Apply planned writes to a snapshot as the API would (a clear removes the value)."""
    for w in result.writes:
        vals = snap["items"][w["issue"]]["places"][w["place"]]["values"]
        if w["value"] is None:
            vals.pop(w["field"], None)
        else:
            vals[w["field"]] = [w["value"], "t-new"]


def test_plans_converge(mk):
    cases = [
        mk.item(b1={"Status": ("TBD", "t1")}, b2={"Status": ("Import", "t2")}),
        mk.item(b1={"Priority": ("Priority: normal", "t3")}, b2={}, b7={}, issue={"Priority": ("High", "t1")}),
        mk.item(b1={"Sub-team": ("Co-Leads", "t1")}, b2={}),
        mk.item(b2={"Workload": ("XS", "t1")}, b7={}, issue={"Effort": ("High", None)}),
        mk.item(b1={"Start date": ("2026-01-01", "t1")}, b2={"Start date": ("2026-02-01", "t2")}, b7={}, issue={}),
    ]
    s = mk.snap(cases)
    r = core.plan(s, {})
    assert r.writes
    apply_to(s, r)
    r2 = core.plan(s, r.state)
    assert r2.writes == [] and r2.conflicts == []


def test_read_failure_keeps_details_out_of_the_log(monkeypatch, tmp_path):
    def boom(clients):
        raise github.GraphQLError('[{"message": "Could not resolve node PVTI_secret"}]')

    monkeypatch.setattr(github, "read_all", boom)
    out = io.StringIO()
    report = tmp_path / "r.txt"
    assert cli.run(args(tmp_path, summary=True, report=str(report)), {}, out) == 2
    assert out.getvalue().strip() == "read failed: GraphQLError"
    assert "PVTI_secret" in report.read_text()


def test_summary_hides_conflicts_and_skips(mk, monkeypatch, tmp_path):
    s = mk.snap(
        [
            mk.item(
                b1={"Category": ("Tech", "t1")}, b2={"Category": ("Science", "t2"), "Release": ("Interra 2099", "t2")}
            )
        ]
    )
    s["fields"]["b2"]["Release"]["options"]["Interra 2099"] = "z"
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    out = io.StringIO()
    cli.run(args(tmp_path, summary=True), {}, out)
    text = out.getvalue()
    assert "conflicts: 1" in text and "skipped: 1" in text
    assert "github.com" not in text and "Science" not in text and "Interra 2099" not in text


def test_crash_during_writes_saves_state(mk, monkeypatch, tmp_path):
    s = mk.snap([mk.item(b1={}, b2={"Category": ("Tech", "t1"), "Start date": ("2026-01-01", "t1")})])
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    calls = []

    def write(clients, snap, w):
        calls.append(w)
        if len(calls) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(github, "write", write)
    try:
        cli.run(args(tmp_path, apply=True), {}, io.StringIO())
    except KeyboardInterrupt:
        pass
    state = cli.load_state(tmp_path / "state.json")
    seen = {f: state["items"]["I1"][f]["seen"]["b1"] for f in ("start", "category")}
    assert seen == {"start": "2026-01-01", "category": None}  # first write kept, second left for later
    assert not (tmp_path / "state.json.tmp").exists()


def test_stop_after_failures_in_a_row(mk, monkeypatch, tmp_path):
    items = [mk.item(b1={}, b2={"Category": ("Tech", "t1")}) for _ in range(cli.STOP_AFTER_FAILURES + 3)]
    monkeypatch.setattr(github, "read_all", lambda clients: mk.snap(items))
    calls = []

    def refuse(clients, snap, w):
        calls.append(w)
        raise github.GraphQLError("x")

    monkeypatch.setattr(github, "write", refuse)
    out = io.StringIO()
    assert cli.run(args(tmp_path, apply=True, max_changes=99), {}, out) == 1
    assert len(calls) == cli.STOP_AFTER_FAILURES
    assert "stopped after 5 failed writes in a row" in out.getvalue() and "deferred: 3 writes" in out.getvalue()


def test_damaged_state_stops_the_run(mk, monkeypatch, tmp_path):
    p = tmp_path / "state.json"
    for text in ('{"items": ', "[]"):
        p.write_text(text)
        with pytest.raises(cli.StateError):
            cli.load_state(p)
    monkeypatch.setattr(github, "read_all", lambda clients: mk.snap([]))
    out = io.StringIO()
    assert cli.run(args(tmp_path, summary=True), {}, out) == 2
    assert out.getvalue().strip() == "run failed: StateError"


def test_bulk_mark_clears_when_drained(mk, monkeypatch, tmp_path):
    s = mk.snap([mk.item(b1={}, b2={"Category": ("Tech", "t1"), "Start date": ("2026-01-01", "t1")})])
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    monkeypatch.setattr(github, "write", lambda c, snap, w: apply_to(snap, type("R", (), {"writes": [w]})))
    cli.run(args(tmp_path, apply=True, max_changes=0, limit=1, allow_bulk=True), {}, io.StringIO())
    assert len(cli.load_state(tmp_path / "state.json")["bulk_keys"]) == 1
    cli.run(args(tmp_path, apply=True, max_changes=0, limit=1), {}, io.StringIO())
    assert cli.load_state(tmp_path / "state.json")["bulk_keys"] == []


def test_plain_deferral_does_not_approve_a_bulk(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 3))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    monkeypatch.setattr(github, "write", lambda c, snap, w: apply_to(snap, type("R", (), {"writes": [w]})))
    assert cli.run(args(tmp_path, apply=True, limit=1), {}, io.StringIO()) == 0
    assert cli.load_state(tmp_path / "state.json")["bulk_keys"] == []


def many(mk, n, field="Category", value="Tech"):
    return [mk.item(b1={}, b2={field: (value, "t1")}) for _ in range(n)]


def test_bulk_approval_does_not_cover_a_new_mass_change(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 6))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    monkeypatch.setattr(github, "write", lambda c, snap, w: apply_to(snap, type("R", (), {"writes": [w]})))
    cli.run(args(tmp_path, apply=True, max_changes=1, limit=2, allow_bulk=True), {}, io.StringIO())
    assert len(cli.load_state(tmp_path / "state.json")["bulk_keys"]) == 4
    # A new change of the same size as the remainder is not covered by the approval.
    for n, it in enumerate(many(mk, 4, "Release", "Interra 2027")):
        s["items"][f"N{n}"] = it
    out = io.StringIO()
    assert cli.run(args(tmp_path, apply=True, max_changes=1, limit=2), {}, out) == 3
    assert "stop: 8 writes" in out.getvalue()


def test_bulk_approval_ends_after_idle_runs(mk, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "PARK_AFTER", 99)  # no parking, so only the idle rule can end the approval
    s = mk.snap(many(mk, 4))
    monkeypatch.setattr(github, "read_all", lambda clients: s)

    def refuse(clients, snap, w):
        raise github.GraphQLError("x")

    monkeypatch.setattr(github, "write", refuse)
    cli.run(args(tmp_path, apply=True, max_changes=1, allow_bulk=True), {}, io.StringIO())
    for idle in range(1, cli.BULK_IDLE_RUNS):
        state = cli.load_state(tmp_path / "state.json")
        assert state["bulk_idle"] == idle and len(state["bulk_keys"]) == 4
        # The approval stays while runs fail, so the failures can reach parking.
        assert cli.run(args(tmp_path, apply=True, max_changes=1), {}, io.StringIO()) == 1
    assert cli.load_state(tmp_path / "state.json")["bulk_keys"] == []
    # Writes that failed before are retried without approval; a new mass change still stops.
    assert cli.run(args(tmp_path, apply=True, max_changes=1), {}, io.StringIO()) == 1
    for n, it in enumerate(many(mk, 3, "Release", "Interra 2027")):
        s["items"][f"N{n}"] = it
    assert cli.run(args(tmp_path, apply=True, max_changes=1), {}, io.StringIO()) == 3


def test_persistent_failures_are_parked_and_do_not_block(mk, monkeypatch, tmp_path):
    bad = [mk.item(b1={}, b2={"Category": ("Tech", "t1")}) for _ in range(cli.STOP_AFTER_FAILURES)]
    good = [mk.item(b1={}, b2={"Release": ("Interra 2027", "t1")}) for _ in range(3)]
    s = mk.snap(bad + good)  # the failing writes sort first (I1 .. I5)
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    written = []

    def write(clients, snap, w):
        if w["fkey"] == "category":
            raise github.GraphQLError("locked")
        written.append(w["issue"])
        apply_to(snap, type("R", (), {"writes": [w]}))

    monkeypatch.setattr(github, "write", write)
    cli.run(args(tmp_path, apply=True, max_changes=99), {}, io.StringIO())
    assert written == []  # run 1: the 5 failures at the head stop the run
    cli.run(args(tmp_path, apply=True, max_changes=99), {}, io.StringIO())
    assert sorted(written) == ["I6", "I7", "I8"]  # run 2: writes that failed before go last
    for _ in range(cli.PARK_AFTER - 2):
        cli.run(args(tmp_path, apply=True, max_changes=99), {}, io.StringIO())
    out = io.StringIO()
    assert cli.run(args(tmp_path, apply=True, max_changes=99, report=str(tmp_path / "r.txt")), {}, out) == 4
    assert "parked: 5 writes" in out.getvalue() and "parked https://" in (tmp_path / "r.txt").read_text()
    state = cli.load_state(tmp_path / "state.json")
    assert {r["n"] for r in state["failures"].values()} == {cli.PARK_AFTER}  # parked counts are kept
    assert {state["items"][f"I{n}"]["category"]["seen"]["b1"] for n in range(1, 6)} == {None}  # not written


def test_failure_count_resets_on_success(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 1))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    outcome = ["fail"]

    def write(clients, snap, w):
        if outcome[0] == "fail":
            raise github.GraphQLError("x")
        apply_to(snap, type("R", (), {"writes": [w]}))

    monkeypatch.setattr(github, "write", write)
    cli.run(args(tmp_path, apply=True), {}, io.StringIO())
    assert [r["n"] for r in cli.load_state(tmp_path / "state.json")["failures"].values()] == [1]
    outcome[0] = "ok"
    cli.run(args(tmp_path, apply=True), {}, io.StringIO())
    assert cli.load_state(tmp_path / "state.json")["failures"] == {}


def test_parked_write_is_tried_again_after_a_day(mk, monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    s = mk.snap(many(mk, 1))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    key = cli.write_key(core.plan(s, {}).writes[0])
    for age, tried in ((timedelta(hours=2), 0), (timedelta(days=2), 1)):
        at = (datetime.now(UTC) - age).isoformat(timespec="seconds")
        (tmp_path / "state.json").write_text(json.dumps({"failures": {key: {"n": cli.PARK_AFTER, "at": at}}}))
        calls = []
        monkeypatch.setattr(github, "write", lambda c, snap, w, calls=calls: calls.append(w))
        assert cli.run(args(tmp_path, apply=True), {}, io.StringIO()) == (4 if tried == 0 else 0)
        assert len(calls) == tried
    assert not cli.is_parked({"n": cli.PARK_AFTER - 1, "at": datetime.now(UTC).isoformat()}, datetime.now(UTC))
    assert not cli.is_parked(None, datetime.now(UTC))


def test_failures_in_a_row_reset_after_a_success(mk, monkeypatch, tmp_path):
    n = 2 * cli.STOP_AFTER_FAILURES
    s = mk.snap(many(mk, n))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    calls = []

    def write(clients, snap, w):
        calls.append(w)
        if len(calls) % cli.STOP_AFTER_FAILURES:
            raise github.GraphQLError("x")

    monkeypatch.setattr(github, "write", write)
    cli.run(args(tmp_path, apply=True, max_changes=99), {}, io.StringIO())
    assert len(calls) == n


@pytest.mark.parametrize("kind", [ValueError, TypeError, RuntimeError])
def test_any_write_error_counts_as_a_failed_write(kind, mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 3))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    calls = []

    def write(clients, snap, w):
        calls.append(w)
        raise kind("x")

    monkeypatch.setattr(github, "write", write)
    out = io.StringIO()
    assert cli.run(args(tmp_path, apply=True), {}, out) == 1
    assert len(calls) == 3 and f"failed: 3 ({kind.__name__} 3)" in out.getvalue()


def test_state_save_keeps_the_old_file_when_writing_fails(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    cli.save_state(p, {"items": {"old": {}}})

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cli.os, "replace", boom)
    with pytest.raises(OSError):
        cli.save_state(p, {"items": {"new": {}}})
    assert cli.load_state(p) == {"items": {"old": {}}}


def test_run_failure_after_the_read_shows_the_type_only(mk, monkeypatch, tmp_path):
    monkeypatch.setattr(github, "read_all", lambda clients: mk.snap([]))

    def boom(snap, state):
        raise KeyError("PVTI_secret")

    monkeypatch.setattr(core, "plan", boom)
    out = io.StringIO()
    assert cli.run(args(tmp_path, summary=True, report=str(tmp_path / "r.txt")), {}, out) == 2
    assert out.getvalue().strip() == "run failed: KeyError"
    assert "PVTI_secret" in (tmp_path / "r.txt").read_text()


def test_sigterm_becomes_an_exit():
    import signal

    with pytest.raises(SystemExit) as exc:
        cli._terminate(signal.SIGTERM, None)
    assert exc.value.code == 128 + signal.SIGTERM


def test_warnings_name_missing_fields_and_unread_items(mk, monkeypatch, tmp_path):
    s = mk.snap([])
    del s["fields"]["b7"]["Workload"]
    s["skipped_items"] = {"b2": 3, "b1": 0}
    s["unread_issues"] = {"FormingWorlds": 2, "Interra-Project": 0}
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    out = io.StringIO()
    cli.run(args(tmp_path, summary=True), {}, out)
    assert "warning: b7 has no field 'Workload'" in out.getvalue()
    assert "note: Interra Collaboration: 3 items are not readable issues" in out.getvalue()
    assert "warning: 2 FormingWorlds issues could not be read" in out.getvalue()
    assert "Interra-Project issues" not in out.getvalue()


def test_resets_are_named_in_the_summary(mk, monkeypatch, tmp_path):
    a = mk.item(b1={"Category": ("Tech", "t1")}, b2={"Category": ("Tech", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    (tmp_path / "state.json").write_text(json.dumps(state))
    s = mk.snap([mk.item(b1={"Category": ("Tech", "t1")}, b2={})], ids={("b2", "Category"): "NEW"})
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    out = io.StringIO()
    cli.run(args(tmp_path, summary=True), {}, out)
    assert "reset: b2 / category is treated as new" in out.getvalue()


def three_runs_with_issue_gone(mk, back_value, back_time):
    """Agreed 05-01; the issue goes; board 1 changes to 06-01 at t2; the issue returns."""
    a = mk.item(b1={"Start date": ("2026-05-01", "t1")}, issue={"Start date": ("2026-05-01", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    s2 = mk.snap([mk.item(b1={"Start date": ("2026-06-01", "t2")})])
    state = core.plan(s2, state).state
    back = mk.item(b1={"Start date": ("2026-06-01", "t2")}, issue={"Start date": (back_value, back_time)})
    return core.plan(mk.snap([back]), state)


def test_returning_place_with_a_newer_value_wins(mk):
    r = three_runs_with_issue_gone(mk, "2026-07-01", "t3")
    assert writes(r) == {("b1", "Start date"): "2026-07-01"}


def test_returning_place_with_an_older_value_receives(mk):
    r = three_runs_with_issue_gone(mk, "2026-07-01", "t0")
    assert writes(r) == {("issue", "Start date"): "2026-06-01"}


def test_returning_place_on_a_reset_field_agrees_without_a_change(mk):
    a = mk.item(b1={"Category": ("Tech", "t1")}, b2={"Category": ("Tech", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    state = core.plan(mk.snap([mk.item(b1={"Category": ("Tech", "t1")})]), state).state
    s = mk.snap(
        [mk.item(b1={"Category": ("Tech", "t1")}, b2={"Category": ("Tech", "t9")})], ids={("b2", "Category"): "NEW"}
    )
    r = core.plan(s, state)
    assert r.writes == [] and r.conflicts == []


def test_place_stays_gone_until_it_has_the_field_again(mk):
    a = mk.item(b1={"Category": ("Tech", "t1")}, b2={"Category": ("Tech", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    state = core.plan(mk.snap([mk.item(b1={"Category": ("Tech", "t1")})]), state).state
    s = mk.snap([mk.item(b1={"Category": ("Tech", "t1")}, b2={})])
    del s["fields"]["b2"]["Category"]  # back on board 2, but the field is missing this run
    state = core.plan(s, state).state
    assert state["items"]["I1"]["category"]["gone"] == ["b2"]
    back = mk.item(b1={"Category": ("Tech", "t1")}, b2={})  # field back, empty: receive, not clear
    assert writes(core.plan(mk.snap([back]), state)) == {("b2", "Category"): "Tech"}


def test_issue_gone_for_long_is_dropped(mk):
    a = mk.item(b1={"Category": ("Tech", "t1")}, b2={"Category": ("Tech", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    s = mk.snap([])
    s["read_at"] = "2026-01-01T00:00:00+00:00"
    state = core.plan(s, state).state
    assert state["items"]["I1"]["category"]["gone_since"] == "2026-01-01T00:00:00+00:00"
    s["read_at"] = "2026-04-01T00:00:00+00:00"  # exactly 90 days
    state = core.plan(s, state).state
    assert "I1" in state["items"]
    s["read_at"] = "2026-04-01T00:00:01+00:00"
    assert "I1" not in core.plan(s, state).state["items"]


def test_deadline_stops_the_writes(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 4))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    a = args(tmp_path, apply=True, deadline=10.0, started=1000.0)
    clock = iter([1000.0, 1009.0, 1011.0, 1012.0])  # checked before each write
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(clock))
    calls = []
    monkeypatch.setattr(github, "write", lambda c, snap, w: calls.append(w))
    out = io.StringIO()
    assert cli.run(a, {}, out) == 0
    assert len(calls) == 2 and "stopped at the time limit" in out.getvalue() and "deferred: 2 writes" in out.getvalue()


def test_report_shows_the_plan_while_writes_run(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 2))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    report = tmp_path / "r.txt"
    seen = []
    monkeypatch.setattr(github, "write", lambda c, snap, w: seen.append(report.read_text()))
    cli.run(args(tmp_path, apply=True, report=str(report)), {}, io.StringIO())
    assert "plan  https://" in seen[0]


def test_sigterm_handler_is_installed_and_restored(monkeypatch):
    import signal

    during = []
    monkeypatch.setattr(cli, "make_clients", lambda env: {})
    monkeypatch.setattr(cli, "run", lambda a, c: during.append(signal.getsignal(signal.SIGTERM)) or 0)
    before = signal.getsignal(signal.SIGTERM)
    assert cli.main([], env={}) == 0
    assert during == [cli._terminate] and signal.getsignal(signal.SIGTERM) == before


def test_exit_during_writes_saves_state(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 2))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    calls = []

    def write(clients, snap, w):
        calls.append(w)
        if len(calls) == 2:
            cli._terminate(15, None)

    monkeypatch.setattr(github, "write", write)
    with pytest.raises(SystemExit):
        cli.run(args(tmp_path, apply=True), {}, io.StringIO())
    state = cli.load_state(tmp_path / "state.json")
    assert [state["items"][i]["category"]["seen"]["b1"] for i in ("I1", "I2")] == ["Tech", None]


def test_failing_report_does_not_print_the_error_text(monkeypatch, tmp_path):
    def boom(clients):
        raise github.GraphQLError("PVTI_secret")

    monkeypatch.setattr(github, "read_all", boom)
    out = io.StringIO()
    bad = tmp_path / "missing" / "r.txt"
    assert cli.run(args(tmp_path, summary=True, report=str(bad)), {}, out) == 2
    assert out.getvalue().strip() == "read failed: GraphQLError"


def test_missing_token_exits_with_code_2():
    with pytest.raises(SystemExit) as exc:
        cli.make_clients({})
    assert exc.value.code == 2


def test_bulk_approval_keeps_failed_writes(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 4))
    monkeypatch.setattr(github, "read_all", lambda clients: s)

    def write(clients, snap, w):
        if w["issue"] == "I1":
            raise github.GraphQLError("x")
        apply_to(snap, type("R", (), {"writes": [w]}))

    monkeypatch.setattr(github, "write", write)
    cli.run(args(tmp_path, apply=True, max_changes=1, limit=2, allow_bulk=True), {}, io.StringIO())
    keys = cli.load_state(tmp_path / "state.json")["bulk_keys"]
    assert len(keys) == 3 and any(k.startswith("I1|") for k in keys)


def test_new_value_on_a_recreated_field_is_a_change(mk):
    a = mk.item(b1={"Category": ("Tech", "t5")}, b2={"Category": ("Tech", "t5")})
    state = core.plan(mk.snap([a]), {}).state
    state = core.plan(mk.snap([mk.item(b1={"Category": ("Tech", "t5")})]), state).state
    s = mk.snap(
        [mk.item(b1={"Category": ("Tech", "t5")}, b2={"Category": ("Science", "t1")})], ids={("b2", "Category"): "NEW"}
    )
    assert writes(core.plan(s, state)) == {("b1", "Category"): "Science"}


def test_a_new_value_is_a_new_write_for_failures_and_parking(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 1))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    calls = []

    def refuse(clients, snap, w):
        calls.append(w["value"])
        raise github.GraphQLError("x")

    monkeypatch.setattr(github, "write", refuse)
    for _ in range(cli.PARK_AFTER):
        cli.run(args(tmp_path, apply=True), {}, io.StringIO())
    assert cli.run(args(tmp_path, apply=True), {}, io.StringIO()) == 4  # parked
    s["items"]["I1"]["places"]["b2"]["values"]["Category"] = ["Science", "t2"]
    assert cli.run(args(tmp_path, apply=True), {}, io.StringIO()) == 1  # tried at once
    assert calls[-1] == "Science"
    assert sorted(r["n"] for r in cli.load_state(tmp_path / "state.json")["failures"].values()) == [1]


def test_parked_writes_stay_approved_and_do_not_count_as_new(mk, monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    s = mk.snap(many(mk, 3))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    keys = [cli.write_key(w) for w in core.plan(s, {}).writes]
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    state = {"failures": {k: {"n": cli.PARK_AFTER, "at": old} for k in keys}}
    (tmp_path / "state.json").write_text(json.dumps(state))
    calls = []
    monkeypatch.setattr(github, "write", lambda c, snap, w: calls.append(w))
    # 3 retried writes against a limit of 1: they failed before, so they are not a new mass change.
    assert cli.run(args(tmp_path, apply=True, max_changes=1), {}, io.StringIO()) == 0
    assert len(calls) == 3


def test_parked_writes_keep_their_bulk_approval(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 3))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    monkeypatch.setattr(cli, "STOP_AFTER_FAILURES", 99)

    def refuse(clients, snap, w):
        raise github.GraphQLError("x")

    monkeypatch.setattr(github, "write", refuse)
    cli.run(args(tmp_path, apply=True, max_changes=0, allow_bulk=True), {}, io.StringIO())
    for _ in range(cli.PARK_AFTER):
        cli.run(args(tmp_path, apply=True, max_changes=0), {}, io.StringIO())
    state = cli.load_state(tmp_path / "state.json")
    assert len(state["bulk_keys"]) == 3


def test_reaching_parking_counts_as_progress_and_reapproval_resets_idle(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 2 * cli.STOP_AFTER_FAILURES))  # 10 writes that always fail
    monkeypatch.setattr(github, "read_all", lambda clients: s)

    def refuse(clients, snap, w):
        raise github.GraphQLError("x")

    monkeypatch.setattr(github, "write", refuse)
    cli.run(args(tmp_path, apply=True, max_changes=1, allow_bulk=True), {}, io.StringIO())
    idles = []
    for _ in range(2 * cli.PARK_AFTER):
        assert cli.run(args(tmp_path, apply=True, max_changes=1), {}, io.StringIO()) != 3
        idles.append(cli.load_state(tmp_path / "state.json")["bulk_idle"])
    assert 0 in idles[cli.PARK_AFTER - 2 :]  # the run that parks a block resets the idle count
    state = cli.load_state(tmp_path / "state.json")
    state["bulk_idle"] = cli.BULK_IDLE_RUNS - 1
    (tmp_path / "state.json").write_text(json.dumps(state))
    cli.run(args(tmp_path, apply=True, max_changes=1, allow_bulk=True), {}, io.StringIO())
    assert cli.load_state(tmp_path / "state.json")["bulk_idle"] <= 1


def test_returning_place_without_a_known_agreed_time_receives(mk):
    a = mk.item(b1={"Start date": ("2026-05-01", None)}, issue={"Start date": ("2026-05-01", None)})
    state = core.plan(mk.snap([a]), {}).state
    assert state["items"]["I1"]["start"]["t"] is None
    state = core.plan(mk.snap([mk.item(b1={"Start date": ("2026-05-01", None)})]), state).state
    back = mk.item(b1={"Start date": ("2026-05-01", None)}, issue={"Start date": ("2026-07-01", "t9")})
    assert writes(core.plan(mk.snap([back]), state)) == {("issue", "Start date"): "2026-05-01"}


def test_snapshot_does_not_change_when_the_boards_do_not(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 1))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    texts = []
    for when in ("2026-01-01T00:00:00+00:00", "2026-01-01T00:30:00+00:00"):
        s["read_at"] = when
        cli.run(args(tmp_path, snapshot_dir=str(tmp_path / "snap")), {}, io.StringIO())
        texts.append((tmp_path / "snap" / "snapshot.json").read_text())
    assert texts[0] == texts[1] and "read_at" not in texts[0]


def test_parked_writes_give_code_4_only_when_applying(mk, monkeypatch, tmp_path):
    from datetime import UTC, datetime

    s = mk.snap(many(mk, 1))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    key = cli.write_key(core.plan(s, {}).writes[0])
    now = datetime.now(UTC).isoformat(timespec="seconds")
    (tmp_path / "state.json").write_text(json.dumps({"failures": {key: {"n": cli.PARK_AFTER, "at": now}}}))
    assert cli.run(args(tmp_path), {}, io.StringIO()) == 0
    assert cli.run(args(tmp_path, apply=True), {}, io.StringIO()) == 4


def test_reapproval_starts_a_fresh_idle_count(mk, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "PARK_AFTER", 99)  # no parking, so no progress at all
    s = mk.snap(many(mk, 3))
    monkeypatch.setattr(github, "read_all", lambda clients: s)

    def refuse(clients, snap, w):
        raise github.GraphQLError("x")

    monkeypatch.setattr(github, "write", refuse)
    (tmp_path / "state.json").write_text('{"bulk_idle": 3, "bulk_keys": []}')
    cli.run(args(tmp_path, apply=True, max_changes=0, allow_bulk=True), {}, io.StringIO())
    state = cli.load_state(tmp_path / "state.json")
    assert state["bulk_idle"] == 1 and len(state["bulk_keys"]) == 3


def test_output_files_are_written_atomically(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 1))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    written = []
    real = cli.write_file
    monkeypatch.setattr(cli, "write_file", lambda path, text: written.append(str(path)) or real(path, text))
    monkeypatch.setattr(github, "write", lambda c, snap, w: None)
    report, snapdir = tmp_path / "r.txt", tmp_path / "snap"
    cli.run(args(tmp_path, apply=True, report=str(report), snapshot_dir=str(snapdir)), {}, io.StringIO())
    assert {str(report), str(snapdir / "snapshot.json"), str(tmp_path / "state.json")} <= set(written)


def test_derived_field_is_listed_when_missing_and_skips_are_reported(mk):
    s = mk.snap([mk.item(b2={"Workload": ("S", "t1")}, issue={})])
    del s["fields"]["issue:Interra-Project"]["Effort"]["options"]["Low"]
    r = core.plan(s, {})
    assert r.skipped == [
        {"issue": "I1", "url": s["items"]["I1"]["url"], "place": "issue", "fkey": "Effort", "value": "Low"}
    ]
    del s["fields"]["issue:FormingWorlds"]["Effort"]
    assert ("issue:FormingWorlds", "Effort") in core.missing_fields(s)


def test_writes_that_failed_before_go_last(mk, monkeypatch, tmp_path):
    s = mk.snap(many(mk, 3))
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    first = cli.write_key(core.plan(s, {}).writes[0])
    (tmp_path / "state.json").write_text(json.dumps({"failures": {first: {"n": 1, "at": "2026-01-01T00:00:00+00:00"}}}))
    order = []
    monkeypatch.setattr(github, "write", lambda c, snap, w: order.append(cli.write_key(w)))
    cli.run(args(tmp_path, apply=True), {}, io.StringIO())
    assert order[-1] == first and len(order) == 3


def test_workload_without_an_effort_mapping_writes_no_effort(mk, monkeypatch):
    monkeypatch.setitem(config.DERIVED["effort"], "map", {"XL": "High"})
    s = mk.snap([mk.item(b2={"Workload": ("S", "t1")}, issue={})])
    r = core.plan(s, {})
    assert r.writes == [] and r.skipped == []


def test_skip_line_names_the_derived_field(mk, monkeypatch, tmp_path):
    s = mk.snap([mk.item(b2={"Workload": ("S", "t1")}, issue={})])
    del s["fields"]["issue:Interra-Project"]["Effort"]["options"]["Low"]
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    out = io.StringIO()
    cli.run(args(tmp_path), {}, out)
    assert "issue Effort: no option 'Low'" in out.getvalue()


B1 = {"Category": ("Tech", "t1"), "Start date": ("2026-01-01", "t1")}


def two_values(mk, **extra):
    return mk.item(
        b1={"Category": ("Tech", "t1"), "Start date": ("2026-01-01", "t1")},
        b2={"Category": ("Tech", "t1"), "Start date": ("2026-01-01", "t1")},
        b7={"Start date": ("2026-01-01", "t1")},
        **extra,
    )


def test_place_with_all_values_lost_is_held_back(mk):
    state = core.plan(mk.snap([two_values(mk)]), {}).state
    lost = mk.item(b1=B1, b2={}, b7={"Start date": ("2026-01-01", "t1")})
    r = core.plan(mk.snap([lost]), state)
    assert r.writes == [] and r.held == [("I1", "b2")]
    assert config.WIPE_MIN == 2  # the README states it
    # Read in full again: no change. Read empty again: the place receives the agreed values.
    again = core.plan(mk.snap([two_values(mk)]), r.state)
    assert again.writes == [] and again.held == []
    refill = core.plan(mk.snap([lost]), r.state)
    assert writes(refill) == {("b2", "Category"): "Tech", ("b2", "Start date"): "2026-01-01"} and refill.held == []


def test_place_with_one_value_cleared_is_not_held_back(mk):
    a = mk.item(b1={"Category": ("Tech", "t1")}, b2={"Category": ("Tech", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    r = core.plan(mk.snap([mk.item(b1={"Category": ("Tech", "t1")}, b2={})]), state)
    assert writes(r) == {("b1", "Category"): None} and r.held == []


def test_place_with_a_value_left_or_a_new_value_is_not_held_back(mk):
    state = core.plan(mk.snap([two_values(mk)]), {}).state
    one_left = mk.item(b1=B1, b2={"Category": ("Tech", "t1")}, b7={"Start date": ("2026-01-01", "t1")})
    assert core.plan(mk.snap([one_left]), state).held == []
    new = mk.item(b1=B1, b2={"Release": ("Interra 2027", "t2")}, b7={"Start date": ("2026-01-01", "t1")})
    assert core.plan(mk.snap([new]), state).held == []


def test_reset_fields_do_not_count_for_held_back(mk):
    state = core.plan(mk.snap([two_values(mk)]), {}).state
    s = mk.snap([mk.item(b1=B1, b2={}, b7={"Start date": ("2026-01-01", "t1")})])
    s["fields"]["b2"]["Category"]["id"] = "NEW"  # created again: only Start date counts
    r = core.plan(s, state)
    assert r.held == [] and ("b2", "category") in r.resets


def test_held_back_place_is_named_in_the_report(mk, monkeypatch, tmp_path):
    state = core.plan(mk.snap([two_values(mk)]), {}).state
    (tmp_path / "state.json").write_text(json.dumps(state))
    lost = mk.snap([mk.item(b1=B1, b2={}, b7={"Start date": ("2026-01-01", "t1")})])
    monkeypatch.setattr(github, "read_all", lambda clients: lost)
    out, report = io.StringIO(), tmp_path / "r.txt"
    assert cli.run(args(tmp_path, summary=True, report=str(report)), {}, out) == 0
    assert "held back: 1 issue places" in out.getvalue() and "github.com" not in out.getvalue()
    assert "held back https://github.com/Interra-Project/repo/issues/1 Interra Collaboration" in report.read_text()


def test_apply_run_too_soon_after_the_last_stops_before_the_read(mk, monkeypatch, tmp_path):
    from datetime import timedelta

    s = mk.snap(many(mk, 1))
    reads = []
    monkeypatch.setattr(github, "read_all", lambda clients: reads.append(1) or s)
    monkeypatch.setattr(github, "write", lambda c, snap, w: None)
    first = args(tmp_path, apply=True, min_interval=50.0)
    assert cli.run(first, {}, io.StringIO()) == 0
    stamp = json.loads((tmp_path / "state.json").read_text())["applied_at"]
    assert stamp == first.started_at.isoformat(timespec="seconds")
    for gap, code in ((49, 5), (-1, 5), (51, 0)):
        later = args(tmp_path, apply=True, min_interval=50.0, started_at=first.started_at + timedelta(minutes=gap))
        out = io.StringIO()
        assert cli.run(later, {}, out) == code
        if code == 5:
            assert out.getvalue() == f"skipped: the last apply run started at {stamp}, less than 50 minutes ago\n"
            assert len(reads) == 1
        first = later if code == 0 else first
    assert cli.run(args(tmp_path, min_interval=50.0), {}, io.StringIO()) == 0  # a plan without apply is not limited
    assert cli.run(args(tmp_path, apply=True, min_interval=0.0), {}, io.StringIO()) == 0


def test_too_soon_check_on_a_damaged_state_fails_the_run(monkeypatch, tmp_path):
    (tmp_path / "state.json").write_text("[1]")
    monkeypatch.setattr(github, "read_all", lambda clients: pytest.fail("read after a state error"))
    out = io.StringIO()
    assert cli.run(args(tmp_path, apply=True, min_interval=50.0, summary=True), {}, out) == 2
    assert out.getvalue().strip() == "run failed: StateError"


def test_min_interval_defaults_to_50_minutes(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "make_clients", lambda env: {})
    monkeypatch.setattr(cli, "run", lambda a, c: seen.append(a) or 0)
    assert cli.main([], env={}) == 0
    assert seen[0].min_interval == 50.0 and seen[0].started_at.tzinfo is not None
