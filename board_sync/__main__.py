"""Sync shared fields across the Interra boards, the PROTEUS board and the org issue fields.

Without ``--apply`` the run changes nothing on GitHub: it prints the plan and
writes only the snapshot and report files it is given. With ``--apply`` it
writes the plan, unless the plan has more than ``--max-changes`` writes; then
it stops with exit code 3. A person reads the plan and reruns with
``--allow-bulk``; the state then holds the approved writes, and the next runs
write them in batches of ``--limit``. A run with ``--apply`` that starts less
than ``--min-interval`` minutes after the start of the last one stops before
the read, with exit code 5.
"""

import argparse
import io
import json
import os
import signal
import sys
import time
import traceback
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config, core, github

TOKEN_ENV = {
    "Interra-Project": "BOARD_SYNC_TOKEN_INTERRA_PROJECT",
    "FormingWorlds": "BOARD_SYNC_TOKEN_FORMINGWORLDS",
}

# Failed writes in a row after which a run stops and leaves the rest for later.
STOP_AFTER_FAILURES = 5
# Runs a write may fail before it is parked; a parked write is tried once a day.
PARK_AFTER = 3
PARK_RETRY = timedelta(days=1)
# Runs in a row without a successful write after which a bulk approval ends.
BULK_IDLE_RUNS = PARK_AFTER + 1


class StateError(RuntimeError):
    """The state file cannot be used."""


def make_clients(env=os.environ):
    """Build one GraphQL client per org from the environment.

    Parameters
    ----------
    env : mapping
        Environment; each org reads its own variable and falls back to
        ``GH_TOKEN``.

    Returns
    -------
    dict
        Org login to ``github.Client``.

    Raises
    ------
    SystemExit
        With code 2 if an org has no token.
    """
    clients = {}
    for org, var in TOKEN_ENV.items():
        token = env.get(var) or env.get("GH_TOKEN")
        if not token:
            print(f"no token for {org}: set {var} or GH_TOKEN", file=sys.stderr)
            raise SystemExit(2)
        clients[org] = github.Client(token)
    return clients


def load_state(path):
    """Read the state file; an absent or empty file is an empty state.

    Raises
    ------
    StateError
        If the file is not a JSON object, so a damaged state never counts as a first run.
    """
    p = Path(path)
    if not p.exists() or not p.read_text().strip():
        return {}
    try:
        data = json.loads(p.read_text())
    except ValueError:
        raise StateError(f"state file {path} is not valid JSON") from None
    if not isinstance(data, dict):
        raise StateError(f"state file {path} does not hold a JSON object")
    return data


def write_file(path, text):
    """Write a file through a temporary file, so a killed run leaves the old file whole."""
    tmp = Path(f"{path}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def save_state(path, state):
    """Write the state file atomically."""
    write_file(path, json.dumps(state, indent=1, sort_keys=True))


def describe(w):
    """Format one planned write as one line."""
    where = config.BOARDS.get(w["place"], {}).get("title", "issue field")
    value = "(clear)" if w["value"] is None else w["value"]
    return f"{w['url']}  {where} / {w['field']}: {value}"


def counts(writes):
    """Count writes per place and field, as lines without issue data."""
    tally = Counter(f"{config.BOARDS.get(w['place'], {}).get('title', 'issue field')} / {w['field']}" for w in writes)
    return [f"  {n:5d}  {k}" for k, n in sorted(tally.items())]


def warnings(snap):
    """Lines about fields and items the run could not use, without issue data."""
    lines = [f"warning: {key} has no field {name!r}" for key, name in core.missing_fields(snap)]
    for key, n in sorted(snap.get("skipped_items", {}).items()):
        if n:
            lines.append(f"note: {config.BOARDS[key]['title']}: {n} items are not readable issues")
    for org, n in sorted(snap.get("unread_issues", {}).items()):
        if n:
            lines.append(f"warning: {n} {org} issues could not be read for their issue fields")
    return lines


def emit(args, summary, detail, out):
    """Write the full report file and print the summary, or everything."""
    if args.report:
        write_file(args.report, "\n".join(summary + detail) + "\n")
    print("\n".join(summary if args.summary else summary + detail), file=out)


def write_key(w):
    """Key of one planned write, for failure counts and bulk approval."""
    return f"{w['issue']}|{w['place']}|{w['field']}|{w['value']}"


def is_parked(record, now):
    """A write is parked after ``PARK_AFTER`` failures, until a day after its last one."""
    if not record or record["n"] < PARK_AFTER:
        return False
    return now - datetime.fromisoformat(record["at"]) < PARK_RETRY


def apply(args, clients, snap, result, active, parked, state, now, summary, detail):
    """Write the planned values and save the state, also when the run is cut short.

    Parameters
    ----------
    active, parked : list of dict
        Writes to attempt, and writes held back after repeated failures.
    state : dict
        State of the previous run (failure records and bulk approval).
    now : datetime
        Time of this run, for the failure records.

    Returns
    -------
    int
        0 when every attempted write succeeded, else 1.
    """
    old_fails = state.get("failures", {})
    batch, later = active[: args.limit], active[args.limit :]
    failed, done_keys, done, in_row, late = [], set(), 0, 0, False
    try:
        for n, w in enumerate(batch):
            if in_row >= STOP_AFTER_FAILURES:
                break
            if time.monotonic() - args.started > args.deadline:
                late = True
                break
            if n and args.pause:
                time.sleep(args.pause)
            try:
                github.write(clients, snap, w)
                done_keys.add(write_key(w))
                in_row = 0
            except Exception as exc:  # noqa: BLE001  one refused write must not stop the others
                failed.append((w, type(exc).__name__))
                in_row += 1
                detail.append(f"failed {describe(w)}: {exc}")
            done = n + 1
    finally:
        rest = batch[done:] + later
        # Failed, unwritten and parked places keep their old record, so a later run plans them again.
        core.undo_failed(result.state, [w for w, _ in failed] + rest + parked)
        stamp = now.isoformat(timespec="seconds")
        fails = {k: v for k, v in old_fails.items() if k in {write_key(w) for w in rest + parked}}
        for w, _ in failed:
            fails[write_key(w)] = {"n": old_fails.get(write_key(w), {}).get("n", 0) + 1, "at": stamp}
        result.state["failures"] = fails
        # A bulk approval covers the approved writes not yet done (parked ones too), until
        # runs stop making progress; a write that reaches parking counts as progress.
        planned = {write_key(w) for w in active + parked}
        base = planned if args.allow_bulk else set(state.get("bulk_keys", []))
        left = sorted((base & planned) - done_keys)
        progress = done_keys or any(r["n"] == PARK_AFTER for r in fails.values() if r["at"] == stamp)
        idle = 0 if progress else (0 if args.allow_bulk else state.get("bulk_idle", 0)) + 1
        keep = left and idle < BULK_IDLE_RUNS
        result.state["bulk_keys"], result.state["bulk_idle"] = (left, idle) if keep else ([], 0)
        result.state["applied_at"] = args.started_at.isoformat(timespec="seconds")
        save_state(args.state, result.state)
    if in_row >= STOP_AFTER_FAILURES:
        summary.append(f"stopped after {in_row} failed writes in a row")
    if late:
        summary.append(f"stopped at the time limit of {args.deadline} s")
    if rest:
        summary.append(f"deferred: {len(rest)} writes to the next run")
    if failed:
        kinds = Counter(kind for _, kind in failed)
        summary.append(f"failed: {len(failed)} ({', '.join(f'{k} {n}' for k, n in sorted(kinds.items()))})")
        return 1
    return 0


def plan_and_apply(args, clients, snap, out):
    """Plan from a snapshot, apply when asked, and report. See ``run``."""
    if args.snapshot_dir:
        # The read time stays out of the file, so an unchanged board gives an unchanged snapshot.
        Path(args.snapshot_dir).mkdir(parents=True, exist_ok=True)
        stored = {k: v for k, v in snap.items() if k != "read_at"}
        write_file(Path(args.snapshot_dir) / "snapshot.json", json.dumps(stored, indent=1, sort_keys=True))
    state = load_state(args.state)
    result = core.plan(snap, state)
    now = datetime.now(UTC)
    fails = state.get("failures", {})
    parked = [w for w in result.writes if is_parked(fails.get(write_key(w)), now)]
    # Writes that failed before go last, so they cannot hold back the others.
    active = sorted(
        (w for w in result.writes if not is_parked(fails.get(write_key(w)), now)), key=lambda w: write_key(w) in fails
    )
    detail = [f"conflict {c['url']} {c['fkey']}: {c['values']} -> {c['chosen']}" for c in result.conflicts]
    detail += [f"skipped {s['url']} {s['place']} {s['fkey']}: no option {s['value']!r}" for s in result.skipped]
    detail += ["parked " + describe(w) for w in parked]
    detail += ["plan  " + describe(w) for w in active]
    summary = [f"issues: {len(snap['items'])}  writes: {len(active)}  conflicts: {len(result.conflicts)}"]
    summary += counts(active) + warnings(snap)
    summary += [f"reset: {key} / {fkey} is treated as new, its old values are ignored" for key, fkey in result.resets]
    if result.skipped:
        summary.append(f"skipped: {len(result.skipped)} values without an option at the place")
    if result.held:
        summary.append(f"held back: {len(result.held)} issue places with no synced value read, listed in the report")
        detail += [
            f"held back {snap['items'][i]['url']} {config.BOARDS.get(p, {}).get('title', 'issue field')}: no synced value"
            for i, p in result.held
        ]
    if parked:
        summary.append(f"parked: {len(parked)} writes that failed in {PARK_AFTER} runs, listed in the report")
    approved_keys = set(state.get("bulk_keys", []))
    # Writes that already failed were planned before; they never count as a new mass change.
    outside = [w for w in active if write_key(w) not in approved_keys and write_key(w) not in fails]
    approved = args.allow_bulk or len(outside) <= args.max_changes
    code = 0
    if args.apply and len(active) > args.max_changes and not approved:
        summary.append(f"stop: {len(active)} writes exceed --max-changes {args.max_changes}")
        code = 3
    elif args.apply:
        emit(args, summary, detail, io.StringIO())  # the report shows the plan while writes run
        code = apply(args, clients, snap, result, active, parked, state, now, summary, detail)
    emit(args, summary, detail, out)
    return code or (4 if parked and args.apply else 0)


def too_soon(args):
    """Return the start time of the last apply run when it is less than ``args.min_interval`` minutes ago.

    A start time later than this run's start also counts as too soon.
    """
    if not args.apply or args.min_interval <= 0:
        return None
    last = load_state(args.state).get("applied_at")
    if last and args.started_at - datetime.fromisoformat(last) < timedelta(minutes=args.min_interval):
        return last
    return None


def safe_emit(args, line, detail, out):
    """Report a failed run; if even that fails, print the line alone."""
    try:
        emit(args, [line], [detail], out)
    except Exception:  # noqa: BLE001  never let a second error print the first one's text
        print(line, file=out)


def run(args, clients, out=sys.stdout):
    """Read all places, plan, and optionally apply.

    The detail lines name issues and values. With ``args.summary`` the
    stream gets counts only, and the details go to ``args.report`` alone,
    so a public job log shows no board data. An error prints only its type.

    Parameters
    ----------
    args : argparse.Namespace
        Parsed command line.
    clients : dict
        Org login to ``github.Client``.
    out : file
        Stream for the report.

    Returns
    -------
    int
        Exit code: 0 done, 1 some writes failed, 2 the read or the run failed,
        3 too many changes, 4 done but some writes are parked, 5 too soon
        after the last apply run.
    """
    try:
        last = too_soon(args)
    except Exception as exc:  # noqa: BLE001  the summary names the error type only
        safe_emit(args, f"run failed: {type(exc).__name__}", traceback.format_exc(), out)
        return 2
    if last:
        print(f"skipped: the last apply run started at {last}, less than {args.min_interval:g} minutes ago", file=out)
        return 5
    try:
        snap = github.read_all(clients)
    except Exception as exc:  # noqa: BLE001  the summary names the error type only
        safe_emit(args, f"read failed: {type(exc).__name__}", f"read failed: {exc}", out)
        return 2
    try:
        return plan_and_apply(args, clients, snap, out)
    except Exception as exc:  # noqa: BLE001  the summary names the error type only
        safe_emit(args, f"run failed: {type(exc).__name__}", traceback.format_exc(), out)
        return 2


def _terminate(signum, frame):
    """Turn SIGTERM into an exit, so the state is saved on the way out."""
    raise SystemExit(128 + signum)


def main(argv=None, env=os.environ):
    """Parse the command line and run the sync."""
    parser = argparse.ArgumentParser(prog="board_sync", description=__doc__.splitlines()[0])
    parser.add_argument("--state", default="state.json", help="state file of the previous run")
    parser.add_argument("--snapshot-dir", help="folder for the raw snapshot of this run")
    parser.add_argument("--apply", action="store_true", help="write the planned values")
    parser.add_argument("--max-changes", type=int, default=20)
    parser.add_argument("--allow-bulk", action="store_true", help="apply even above --max-changes")
    parser.add_argument("--limit", type=int, default=400, help="most writes in one run; the rest wait")
    parser.add_argument("--pause", type=float, default=0.0, help="seconds between writes")
    parser.add_argument("--deadline", type=float, default=600.0, help="seconds from start after which writes stop")
    parser.add_argument(
        "--min-interval", type=float, default=50.0, help="minutes from the start of the last apply run to the next"
    )
    parser.add_argument("--summary", action="store_true", help="print counts only, no issue data")
    parser.add_argument("--report", help="file for the full report with issue data")
    args = parser.parse_args(argv)
    args.started, args.started_at = time.monotonic(), datetime.now(UTC)
    if env.get("GITHUB_ACTIONS") == "true":
        args.summary = True  # a job log can be public
    old = signal.signal(signal.SIGTERM, _terminate)
    try:
        return run(args, make_clients(env))
    finally:
        signal.signal(signal.SIGTERM, old)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
