"""Tests of the command line: dry run, apply, limits and failures."""

import argparse
import io
import json
import time
from datetime import UTC, datetime

import pytest

import board_sync.__main__ as cli
from board_sync import github


def test_make_clients_reads_env():
    clients = cli.make_clients({"BOARD_SYNC_TOKEN_INTERRA_PROJECT": "a", "GH_TOKEN": "g"})
    assert clients["Interra-Project"].token == "a"
    assert clients["FormingWorlds"].token == "g"
    with pytest.raises(SystemExit):
        cli.make_clients({})


def test_load_state(tmp_path):
    p = tmp_path / "s.json"
    assert cli.load_state(p) == {}
    p.write_text("  ")
    assert cli.load_state(p) == {}
    p.write_text('{"items": {}}')
    assert cli.load_state(p) == {"items": {}}


@pytest.fixture
def world(mk, monkeypatch):
    """Patch the reads to a snapshot with 2 writes due, and record writes."""
    s = mk.snap([mk.item(b1={}, b2={"Category": ("Tech", "t1"), "Start date": ("2026-01-01", "t1")})])
    monkeypatch.setattr(github, "read_all", lambda clients: s)
    done = []

    def fake_write(clients, snap, w):
        done.append(w)
        snap["items"][w["issue"]]["places"][w["place"]]["values"][w["field"]] = [w["value"], "t9"]

    monkeypatch.setattr(github, "write", fake_write)
    return done


def args(tmp_path, **kw):
    base = {
        "state": str(tmp_path / "state.json"),
        "snapshot_dir": None,
        "apply": False,
        "max_changes": 20,
        "allow_bulk": False,
        "limit": 400,
        "pause": 0.0,
        "deadline": 600.0,
        "started": time.monotonic(),
        "started_at": datetime.now(UTC),
        "min_interval": 0.0,
        "summary": False,
        "report": None,
    }
    return argparse.Namespace(**{**base, **kw})


def test_dry_run_writes_nothing(world, tmp_path):
    out = io.StringIO()
    assert cli.run(args(tmp_path, snapshot_dir=str(tmp_path / "snap")), {}, out) == 0
    assert world == []
    assert not (tmp_path / "state.json").exists()
    assert (tmp_path / "snap" / "snapshot.json").exists()
    text = out.getvalue()
    assert "writes: 2" in text and "plan  " in text and "Interra Management / Category: Tech" in text
    assert "      1  Interra Management / Category" in text


def test_apply_writes_and_saves_state(world, tmp_path):
    out = io.StringIO()
    assert cli.run(args(tmp_path, apply=True), {}, out) == 0
    assert len(world) == 2
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["items"]["I1"]["category"]["seen"]["b1"] == "Tech"


def test_apply_stops_above_limit(world, tmp_path):
    out = io.StringIO()
    assert cli.run(args(tmp_path, apply=True, max_changes=1), {}, out) == 3
    assert world == [] and "stop: 2 writes" in out.getvalue()
    assert cli.run(args(tmp_path, apply=True, max_changes=1, allow_bulk=True), {}, io.StringIO()) == 0
    assert len(world) == 2


def test_failed_write_is_retried_next_run(mk, monkeypatch, tmp_path):
    s = mk.snap([mk.item(b1={}, b2={"Category": ("Tech", "t1")})])
    monkeypatch.setattr(github, "read_all", lambda clients: s)

    def refuse(clients, snap, w):
        raise github.GraphQLError("no")

    monkeypatch.setattr(github, "write", refuse)
    out = io.StringIO()
    assert cli.run(args(tmp_path, apply=True), {}, out) == 1
    assert "failed " in out.getvalue()
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["items"]["I1"]["category"]["seen"]["b1"] is None


def test_report_lists_conflicts_and_skips(mk, monkeypatch, tmp_path):
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
    cli.run(args(tmp_path), {}, out)
    assert "conflict " in out.getvalue() and "skipped " in out.getvalue()


def test_describe_clear():
    w = {"url": "u", "place": "issue", "field": "Priority", "value": None}
    assert cli.describe(w) == "u  issue field / Priority: (clear)"


def test_main_parses(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "make_clients", lambda env: {"c": 1})
    monkeypatch.setattr(cli, "run", lambda a, c: seen.update(a=a, c=c) or 0)
    assert cli.main(["--apply", "--max-changes", "5", "--state", "s.json"], env={}) == 0
    assert seen["a"].apply and seen["a"].max_changes == 5 and seen["c"] == {"c": 1} and not seen["a"].summary
    assert cli.main([], env={"GITHUB_ACTIONS": "true"}) == 0
    a = seen["a"]
    assert (a.max_changes, a.limit, a.pause, a.summary, a.apply) == (20, 400, 0.0, True, False)
    assert a.deadline == 600.0 and a.started > 0
    assert cli.main(["--deadline", "5"], env={}) == 0 and seen["a"].deadline == 5.0


def test_limit_defers_the_rest_to_the_next_run(world, monkeypatch, tmp_path):
    pauses = []
    monkeypatch.setattr(cli.time, "sleep", pauses.append)
    out = io.StringIO()
    assert cli.run(args(tmp_path, apply=True, limit=1, pause=0.5), {}, out) == 0
    assert len(world) == 1 and pauses == [] and "deferred: 1 writes" in out.getvalue()
    assert (world[0]["place"], world[0]["field"]) == ("b1", "Start date")
    world.clear()
    # The deferred place keeps its old record; a second plan writes it again.
    assert cli.run(args(tmp_path, apply=True, limit=5, pause=0.5), {}, io.StringIO()) == 0
    assert [(w["place"], w["field"]) for w in world] == [("b1", "Category")] and pauses == []
    assert cli.run(args(tmp_path, apply=True), {}, io.StringIO()) == 0
    assert len(world) == 1  # nothing left to write


def test_pause_between_writes(world, monkeypatch, tmp_path):
    pauses = []
    monkeypatch.setattr(cli.time, "sleep", pauses.append)
    assert cli.run(args(tmp_path, apply=True, pause=0.5), {}, io.StringIO()) == 0
    assert len(world) == 2 and pauses == [0.5]


def test_summary_keeps_issue_data_out_of_the_log(world, tmp_path):
    out = io.StringIO()
    report = tmp_path / "report.txt"
    assert cli.run(args(tmp_path, apply=True, summary=True, report=str(report)), {}, out) == 0
    assert "github.com" not in out.getvalue() and "writes: 2" in out.getvalue()
    assert "plan  https://github.com/Interra-Project/repo/issues/1  Interra Management / Category: Tech" in (
        report.read_text()
    )


def test_summary_counts_failures_by_kind(mk, monkeypatch, tmp_path):
    s = mk.snap([mk.item(b1={}, b2={"Category": ("Tech", "t1")})])
    monkeypatch.setattr(github, "read_all", lambda clients: s)

    def refuse(clients, snap, w):
        raise github.GraphQLError("secret detail")

    monkeypatch.setattr(github, "write", refuse)
    out = io.StringIO()
    assert cli.run(args(tmp_path, apply=True, summary=True), {}, out) == 1
    assert "failed: 1 (GraphQLError 1)" in out.getvalue() and "secret detail" not in out.getvalue()
