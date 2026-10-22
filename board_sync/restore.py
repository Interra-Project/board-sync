"""Set the synced fields back to the values of a stored snapshot.

The restore reads all places now, compares each synced field of each issue
with the stored snapshot, and plans one write per difference: the stored
value, or a clear where the snapshot holds no value. It cannot tell a sync
write from a hand edit, so it also reverts edits made after the snapshot.
Issues, places and fields that are absent from either read are left as they
are and counted in the report.

Without ``--apply`` the run only prints the plan and its plan id, a digest
of the planned writes. ``--apply`` writes only when ``--expect`` names the
plan id of this run, so the writes are the plan that was read. The README
lists the steps around a restore.
"""

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from collections import Counter

from . import config, core, github
from .__main__ import STOP_AFTER_FAILURES, _terminate, counts, describe, make_clients, warnings, write_file


def plan(old, now):
    """Plan the writes that bring the current values back to a stored snapshot.

    Parameters
    ----------
    old : dict
        Stored snapshot to restore.
    now : dict
        Snapshot read at the start of this run.

    Returns
    -------
    tuple
        Writes as dicts with the keys ``issue``, ``url``, ``place``,
        ``field``, ``value`` (``None`` clears) and ``current`` (the value
        now), as ``github.write`` takes them; skipped values as ``(url,
        place, field, value, reason)``; and a ``Counter`` of the issues,
        places and fields left out, by reason.
    """
    writes, skipped, left = [], [], Counter()
    left["issue only in the current read"] = len(set(now["items"]) - set(old["items"]))
    for iid, old_item in sorted(old["items"].items()):
        item = now["items"].get(iid)
        if item is None:
            left["issue only in the stored read"] += 1
            continue
        for place in config.PLACE_ORDER:
            if place not in old_item["places"] or place not in item["places"]:
                if place in old_item["places"] or place in item["places"]:
                    side = "stored" if place in old_item["places"] else "current"
                    left[f"place left out: {core.place_key(item, place)} only in the {side} read"] += 1
                continue
            old_defs = core.place_fields(old, old_item, place)
            defs = core.place_fields(now, item, place)
            for name in core.synced_names(place):
                if name not in old_defs or name not in defs:
                    left[f"field left out: {core.place_key(item, place)} / {name} missing"] += 1
                    continue
                want = core.raw_value(old_item, place, name)[0]
                have = core.raw_value(item, place, name)[0]
                if want == have:
                    continue
                if old_defs[name]["id"] != defs[name]["id"]:
                    skipped.append((item["url"], place, name, want, "field was created again"))
                elif want is not None and defs[name]["type"] == "select" and want not in defs[name]["options"]:
                    skipped.append((item["url"], place, name, want, "no such option"))
                else:
                    writes.append(
                        {
                            "issue": iid,
                            "url": item["url"],
                            "place": place,
                            "field": name,
                            "value": want,
                            "current": have,
                        }
                    )
    return writes, skipped, +left


def plan_id(writes):
    """Return a short digest of the planned writes, independent of their order."""
    keys = sorted(f"{w['issue']}|{w['place']}|{w['field']}|{w['value']}" for w in writes)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:12]


def save(args, lines, out):
    """Write the report file when one is given; return False if it cannot be written."""
    if not args.report:
        return True
    try:
        write_file(args.report, "\n".join(lines) + "\n")
    except OSError as exc:
        print(f"report not written: {exc}", file=out)
        return False
    return True


def apply(args, clients, now, writes, lines):
    """Write the planned values; stop after ``STOP_AFTER_FAILURES`` failures in a row.

    Returns
    -------
    int
        0 when every write succeeded, else 1.
    """
    done, failed, in_row = 0, 0, 0
    try:
        for n, w in enumerate(writes):
            if in_row >= STOP_AFTER_FAILURES:
                lines.append(f"stopped after {in_row} failed writes in a row")
                break
            if n and args.pause:
                time.sleep(args.pause)
            try:
                github.write(clients, now, w)
                in_row = 0
            except Exception as exc:  # noqa: BLE001  one refused write must not stop the others
                failed += 1
                in_row += 1
                lines.append(f"failed {describe(w)}: {exc}")
            done = n + 1
    finally:
        lines.insert(1, f"written: {done - failed}  failed: {failed}  not tried: {len(writes) - done}")
    return 1 if failed else 0


def run(args, clients, out=sys.stdout):
    """Read all places, plan the restore, and apply it when asked.

    The full report goes to ``args.report`` on every path; with ``--apply``
    also before the first write and at the end, when the run is cut short.

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
        0 done, 1 some writes failed or were not tried, 2 the snapshot, the
        read, the plan or the report file failed, 3 ``--expect`` differs from
        the plan id.
    """
    try:
        with open(args.snapshot) as fh:
            old = json.load(fh)
        now = github.read_all(clients)
        writes, skipped, left = plan(old, now)
    except Exception as exc:  # noqa: BLE001  any setup error stops the run before a write
        lines = [f"restore failed: {type(exc).__name__}: {exc}"]
        save(args, lines, out)
        print(lines[0], file=out)
        return 2
    lines = [f"restore writes: {len(writes)}  skipped: {len(skipped)}  plan id: {plan_id(writes)}"]
    lines += counts(writes) + warnings(now)
    lines += [f"note: {n} {why}" for why, n in sorted(left.items())]
    lines += [f"skipped {u} {p} / {f}: {v!r} ({why})" for u, p, f, v, why in skipped]
    lines += [f"restore  {describe(w)}  (now: {w['current']})" for w in writes]
    code = 0
    if args.apply and args.expect != plan_id(writes):
        lines.insert(1, f"stop: --expect {args.expect} differs from the plan id {plan_id(writes)}")
        code = 3
    elif args.apply:
        if not save(args, lines, out):
            return 2
        try:
            code = apply(args, clients, now, writes, lines)
        finally:
            save(args, lines, out)
    print("\n".join(lines), file=out)
    return code if save(args, lines, out) else 2


def main(argv=None, env=os.environ):
    """Parse the command line and run the restore."""
    parser = argparse.ArgumentParser(prog="board_sync.restore", description=__doc__.splitlines()[0])
    parser.add_argument("snapshot", help="stored snapshot.json to restore")
    parser.add_argument("--apply", action="store_true", help="write the planned values")
    parser.add_argument("--expect", help="plan id of the planned writes, required by --apply")
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between writes")
    parser.add_argument("--report", help="file for the full report, required by --apply")
    args = parser.parse_args(argv)
    if args.pause < 0:
        parser.error("--pause must not be negative")
    if args.apply and not args.report:
        parser.error("--apply needs --report")
    old = signal.signal(signal.SIGTERM, _terminate)
    try:
        return run(args, make_clients(env))
    finally:
        signal.signal(signal.SIGTERM, old)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
