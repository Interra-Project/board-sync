"""Tests of the sync decision."""

from board_sync import config, core


def writes(result):
    """Return the writes as ``{(place, field): value}``."""
    return {(w["place"], w["field"]): w["value"] for w in result.writes}


def test_value_maps():
    assert core.to_place("priority", "b1", "Priority 3: standard") == "Priority: normal"
    assert core.to_place("priority", "issue", "Priority 1: critical") == "Urgent"
    assert core.to_place("start", "b1", "2026-01-01") == "2026-01-01"
    assert core.to_place("status", "b1", None) is None
    assert core.to_canon("priority", "b1", "Priority: normal") == "Priority 3: standard"
    # TBD on board 1 stands for 3 canonical values, so it stays TBD.
    assert core.to_canon("status", "b1", "TBD") == "TBD"
    assert core.to_canon("subteam", "b1", "Co-Leads") == "Co-leads"
    assert core.to_canon("status", "b1", None) is None


def test_scope_needs_item_and_field(mk):
    s = mk.snap([mk.item(b2={}, issue={})])
    it = s["items"]["I1"]
    assert core.scope(s, it, "start") == ["issue", "b2"]
    del s["fields"]["b2"]["Start date"]
    assert core.scope(s, it, "start") == ["issue"]
    it["org"] = "someone"  # a user repo has no org issue fields
    assert core.scope(s, it, "start") == []


def test_first_run_fills_empty_places(mk):
    s = mk.snap([mk.item(b1={}, b2={"Target date": ("2026-12-31", "2026-10-01T00:00:00Z")}, b7={}, issue={})])
    r = core.plan(s, {})
    assert writes(r) == {
        ("b1", "Target date"): "2026-12-31",
        ("b7", "Target date"): "2026-12-31",
        ("issue", "Target date"): "2026-12-31",
    }
    assert r.conflicts == []
    assert r.state["items"]["I1"]["target"]["c"] == "2026-12-31"
    assert r.state["fields"]["b2"]["Target date"] == "b2/Target date"


def test_first_run_all_empty_writes_nothing(mk):
    r = core.plan(mk.snap([mk.item(b1={}, b2={})]), {})
    assert r.writes == []
    assert r.state["items"]["I1"]["status"] == {"c": None, "t": None, "seen": {"b2": None, "b1": None}, "gone": []}


def test_first_run_newest_wins_with_conflict(mk):
    s = mk.snap(
        [
            mk.item(
                b1={"Start date": ("2025-12-27", "2025-12-29T00:00:00Z")},
                b2={"Start date": ("2027-07-01", "2026-10-14T00:00:00Z")},
            )
        ]
    )
    r = core.plan(s, {})
    assert writes(r) == {("b1", "Start date"): "2027-07-01"}
    assert r.conflicts[0]["values"] == {"b2": "2027-07-01", "b1": "2025-12-27"}


def test_tie_goes_by_place_order(mk):
    t = "2026-01-01T00:00:00Z"
    s = mk.snap([mk.item(b1={"Category": ("Tech", t)}, b2={"Category": ("Science", t)})])
    assert writes(core.plan(s, {})) == {("b1", "Category"): "Science"}


def test_issue_time_unknown_loses_to_board(mk):
    s = mk.snap(
        [mk.item(b2={"Priority": ("Priority 2: high", "2026-01-01T00:00:00Z")}, issue={"Priority": ("Low", None)})]
    )
    assert writes(core.plan(s, {})) == {("issue", "Priority"): "High"}


def test_lossy_status_agrees_without_conflict(mk):
    s = mk.snap(
        [
            mk.item(
                b1={"Status": ("TBD", "2026-10-13T00:00:00Z")},
                b2={"Status": ("Starter Projects", "2026-01-01T00:00:00Z")},
            )
        ]
    )
    r = core.plan(s, {})
    assert r.writes == [] and r.conflicts == []
    assert r.state["items"]["I1"]["status"]["c"] == "Starter Projects"


def test_mapped_values_agree(mk):
    s = mk.snap(
        [
            mk.item(
                b1={"Priority": ("Priority: normal", "2026-01-02T00:00:00Z")},
                b2={"Priority": ("Priority 3: standard", "2026-01-01T00:00:00Z")},
                issue={"Priority": ("Medium", None)},
            )
        ]
    )
    r = core.plan(s, {})
    assert r.writes == [] and r.conflicts == []


def run_twice(mk, first, second):
    """Plan ``first`` from scratch, then plan ``second`` against its state."""
    state = core.plan(mk.snap([first]), {}).state
    return core.plan(mk.snap([second]), state)


def test_unchanged_second_run_writes_nothing(mk):
    it = mk.item(b1={"Start date": ("2026-01-01", "t1")}, b2={"Start date": ("2026-01-01", "t1")})
    assert run_twice(mk, it, it).writes == []


def test_change_at_one_place_propagates(mk):
    a = mk.item(b1={"Start date": ("2026-01-01", "t1")}, b2={"Start date": ("2026-01-01", "t1")})
    b = mk.item(b1={"Start date": ("2026-01-01", "t1")}, b2={"Start date": ("2026-02-01", "t2")})
    r = run_twice(mk, a, b)
    assert writes(r) == {("b1", "Start date"): "2026-02-01"}
    assert r.state["items"]["I1"]["start"]["seen"] == {"b2": "2026-02-01", "b1": "2026-02-01"}


def test_clear_propagates(mk):
    a = mk.item(b1={"Category": ("Tech", "t1")}, b2={"Category": ("Tech", "t1")})
    b = mk.item(b1={"Category": ("Tech", "t1")}, b2={})
    assert writes(run_twice(mk, a, b)) == {("b1", "Category"): None}


def test_two_changes_newest_wins(mk):
    a = mk.item(b1={"Category": ("Tech", "2026-01-01")}, b2={"Category": ("Tech", "2026-01-01")})
    b = mk.item(b1={"Category": ("Science", "2026-03-01")}, b2={"Category": ("Management", "2026-02-01")})
    r = run_twice(mk, a, b)
    assert writes(r) == {("b2", "Category"): "Science"}
    assert r.conflicts[0]["chosen"] == "Science"


def test_new_place_empty_receives(mk):
    a = mk.item(b2={"Workload": ("M", "t1")}, b1={"Workload": ("M", "t1")})
    b = mk.item(b2={"Workload": ("M", "t1")}, b1={"Workload": ("M", "t1")}, b7={})
    assert writes(run_twice(mk, a, b)) == {("b7", "Workload"): "M"}


def test_new_place_with_value_counts_as_change(mk):
    a = mk.item(b2={"Workload": ("M", "t1")}, b1={"Workload": ("M", "t1")})
    b = mk.item(b2={"Workload": ("M", "t1")}, b1={"Workload": ("M", "t1")}, b7={"Workload": ("XL", "t9")})
    assert writes(run_twice(mk, a, b)) == {("b2", "Workload"): "XL", ("b1", "Workload"): "XL"}


def test_new_place_with_agreeing_value_is_no_change(mk):
    a = mk.item(b2={"Workload": ("M", "t1")}, b1={"Workload": ("M", "t1")})
    b = mk.item(b2={"Workload": ("M", "t1")}, b1={"Workload": ("M", "t1")}, b7={"Workload": ("M", "t9")})
    assert run_twice(mk, a, b).writes == []


def test_out_of_scope_places_keep_their_record(mk):
    a = mk.item(b1={"Category": ("Tech", "t1")}, b2={"Category": ("Tech", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    s = mk.snap([mk.item(b1={"Category": ("Tech", "t1")}, b2={})])
    del s["fields"]["b2"]["Category"]  # item still on board 2, field gone
    rec = core.plan(s, state).state["items"]["I1"]["category"]
    assert rec["seen"] == {"b1": "Tech", "b2": "Tech"} and rec["gone"] == []
    s = mk.snap([mk.item(b1={"Category": ("Tech", "t1")})])  # item left board 2
    rec = core.plan(s, state).state["items"]["I1"]["category"]
    assert rec["seen"] == {"b1": "Tech", "b2": "Tech"} and rec["gone"] == ["b2"]


def test_recreated_field_does_not_clear(mk):
    a = mk.item(b1={"Target date": ("2026-05-01", "t1")}, b2={"Target date": ("2026-05-01", "t1")})
    state = core.plan(mk.snap([a]), {}).state
    s = mk.snap([mk.item(b1={"Target date": ("2026-05-01", "t1")}, b2={})], ids={("b2", "Target date"): "NEW"})
    r = core.plan(s, state)
    assert writes(r) == {("b2", "Target date"): "2026-05-01"}


def test_mass_empty_field_does_not_clear(mk, monkeypatch):
    monkeypatch.setattr(config, "RESET_MIN", 2)
    full = [mk.item(b1={"Start date": ("2026-05-01", "t")}, b2={"Start date": ("2026-05-01", "t")}) for _ in range(2)]
    state = core.plan(mk.snap(full), {}).state
    empty = [mk.item(b1={"Start date": ("2026-05-01", "t")}, b2={}) for _ in range(2)]
    r = core.plan(mk.snap(empty), state)
    assert sorted(writes(r).values()) == ["2026-05-01"]
    assert len(r.writes) == 2


def test_missing_option_is_skipped(mk):
    s = mk.snap([mk.item(b1={}, b2={"Release": ("Interra 2028+", "t1")})])
    s["fields"]["b2"]["Release"]["options"]["Interra 2028+"] = "x"
    r = core.plan(s, {})
    assert r.writes == []
    assert r.skipped == [
        {"issue": "I1", "url": s["items"]["I1"]["url"], "place": "b1", "fkey": "release", "value": "Interra 2028+"}
    ]
    assert r.state["items"]["I1"]["release"]["seen"]["b1"] is None


def test_effort_follows_workload(mk):
    s = mk.snap([mk.item(b2={"Workload": ("XL", "t1")}, issue={"Effort": ("Low", None)})])
    assert writes(core.plan(s, {})) == {("issue", "Effort"): "High"}


def test_effort_untouched_without_workload_or_match(mk):
    s = mk.snap([mk.item(b2={}, issue={"Effort": ("Low", None)})])
    assert core.plan(s, {}).writes == []
    s = mk.snap([mk.item(b2={"Workload": ("S", "t1")}, issue={"Effort": ("Low", None)})])
    assert core.plan(s, {}).writes == []
    s = mk.snap([mk.item(b2={"Workload": ("S", "t1")}, issue={})])
    del s["fields"]["issue:Interra-Project"]["Effort"]["options"]["Low"]
    assert core.plan(s, {}).writes == []
    s = mk.snap([mk.item(b2={"Workload": ("S", "t1")})])  # no issue place
    assert core.plan(s, {}).writes == []


def test_undo_failed_restores_seen(mk):
    s = mk.snap([mk.item(b1={}, b2={"Category": ("Tech", "t1")}, issue={})])
    r = core.plan(s, {})
    failed = [w for w in r.writes if w["place"] == "b1"]
    core.undo_failed(r.state, failed + [{"fkey": None}, {"fkey": "start", "issue": "I9", "place": "b1"}])
    assert r.state["items"]["I1"]["category"]["seen"]["b1"] is None
    # The next run sees no change at b1 and writes the value again.
    r2 = core.plan(s, r.state)
    assert writes(r2) == {("b1", "Category"): "Tech"}
