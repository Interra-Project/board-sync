"""Sync decision: which value wins for each issue and field, and what to write.

The input is a snapshot of all places (see ``github.read_all``) and the state
of the previous run. The state holds, per issue and field, the agreed
canonical value ``c`` and the canonical value last seen at each place. A place
whose value differs from its last seen value changed since the previous run.
When several places changed, the newest change wins.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import config

_NEW = object()  # marker: the place has no recorded value yet


@dataclass
class Plan:
    """Result of one sync decision over all issues.

    Attributes
    ----------
    writes : list of dict
        One entry per value to write. Keys: ``issue``, ``url``, ``place``,
        ``fkey`` (``None`` for a derived field), ``field``, ``value``
        (``None`` clears) and ``prev`` (the canonical value the place held
        before the write).
    state : dict
        State to store after the writes; ``undo_failed`` restores the
        records of writes that failed or were not made.
    conflicts : list of dict
        Fields where two places held different new values.
    skipped : list of dict
        Values that cannot be written because the place lacks the option.
    resets : list of tuple
        ``(place_key, fkey)`` pairs treated as new fields in this run.
    held : list of tuple
        ``(issue, place)`` pairs held back in this run, see ``held_places``.
    """

    writes: list = field(default_factory=list)
    state: dict = field(default_factory=dict)
    conflicts: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    resets: list = field(default_factory=list)
    held: list = field(default_factory=list)


def to_place(fkey, place, value):
    """Map a canonical value to the vocabulary of one place.

    Parameters
    ----------
    fkey : str
        Canonical field key, a key of ``config.FIELDS``.
    place : str
        Place key.
    value : str or None
        Canonical value.

    Returns
    -------
    str or None
        The value as the place names it.
    """
    if value is None:
        return None
    return config.FIELDS[fkey].get("to_place", {}).get(place, {}).get(value, value)


def to_canon(fkey, place, value):
    """Map a place value back to the canonical vocabulary.

    A place value that more than one canonical value maps to (for example
    ``TBD`` on board 1) stays as it is.

    Parameters
    ----------
    fkey : str
        Canonical field key.
    place : str
        Place key.
    value : str or None
        Value as the place names it.

    Returns
    -------
    str or None
        Canonical value.
    """
    if value is None:
        return None
    mapping = config.FIELDS[fkey].get("to_place", {}).get(place, {})
    sources = [c for c, v in mapping.items() if v == value]
    return sources[0] if len(sources) == 1 else value


def place_fields(snap, item, place):
    """Return the field definitions of one place for one issue.

    Parameters
    ----------
    snap : dict
        Snapshot of all places.
    item : dict
        One entry of ``snap["items"]``.
    place : str
        Place key.

    Returns
    -------
    dict
        Field name to definition; empty if the place has no fields.
    """
    if place == "issue":
        return snap["fields"].get(f"issue:{item.get('org')}", {})
    return snap["fields"].get(place, {})


def scope(snap, item, fkey):
    """List the places that hold this issue and this field.

    Parameters
    ----------
    snap : dict
        Snapshot of all places.
    item : dict
        One entry of ``snap["items"]``.
    fkey : str
        Canonical field key.

    Returns
    -------
    list of str
        Place keys in ``config.PLACE_ORDER`` order.
    """
    names = config.FIELDS[fkey]["places"]
    return [
        p for p in config.PLACE_ORDER if p in names and p in item["places"] and names[p] in place_fields(snap, item, p)
    ]


def raw_value(item, place, name):
    """Return ``(value, changed_at)`` of one field at one place.

    Parameters
    ----------
    item : dict
        One entry of ``snap["items"]``.
    place : str
        Place key.
    name : str
        Field name at that place.

    Returns
    -------
    tuple
        Value (or ``None``) and ISO change time (or ``None``). A board does not
        record when a value was cleared, so an empty board value has no time.
    """
    return tuple(item["places"][place]["values"].get(name, (None, None)))


def place_key(item, place):
    """Return the key of a place's field set: ``issue:<org>`` or the board key."""
    return f"issue:{item.get('org')}" if place == "issue" else place


def field_ids(snap, old=None):
    """Return the id of every field at every place key, for the state.

    Ids of fields missing from this snapshot stay from ``old``, so a field
    that is deleted and created again between two runs is still detected.
    """
    ids = {k: dict(v) for k, v in (old or {}).items()}
    for k, fields in snap["fields"].items():
        ids.setdefault(k, {}).update({name: f["id"] for name, f in fields.items()})
    return ids


def synced_names(place):
    """Return the field names that the sync writes at one place, derived fields included."""
    names = [spec["places"][place] for spec in config.FIELDS.values() if place in spec["places"]]
    return names + [spec["field"] for spec in config.DERIVED.values() if spec["place"] == place]


def missing_fields(snap):
    """List configured fields that a place does not have, as ``(place_key, name)``."""
    out = []
    for key in snap["fields"]:
        place = "issue" if key.startswith("issue:") else key
        out += [(key, name) for name in synced_names(place) if name not in snap["fields"][key]]
    return sorted(set(out))


def reset_places(snap, state):
    """Find place fields that were emptied as a whole or created again.

    A field counts as reset when its id differs from the id in the state
    (deleted and created again), or when it held at least
    ``config.RESET_MIN`` values in the state and holds none now.

    Parameters
    ----------
    snap : dict
        Snapshot of all places.
    state : dict
        State of the previous run.

    Returns
    -------
    set of tuple
        ``(place_key, fkey)`` pairs whose recorded values must be ignored.
    """
    reset, seen, now = set(), {}, {}
    old_ids = state.get("fields", {})
    for key, fields in snap["fields"].items():
        for fkey, spec in config.FIELDS.items():
            place = "issue" if key.startswith("issue:") else key
            name = spec["places"].get(place)
            if name in fields and name in old_ids.get(key, {}) and old_ids[key][name] != fields[name]["id"]:
                reset.add((key, fkey))
    old = state.get("items", {})
    for iid, item in snap["items"].items():
        for fkey, spec in config.FIELDS.items():
            for place in scope(snap, item, fkey):
                key = (place_key(item, place), fkey)
                if raw_value(item, place, spec["places"][place])[0] is not None:
                    now[key] = now.get(key, 0) + 1
                if old.get(iid, {}).get(fkey, {}).get("seen", {}).get(place) is not None:
                    seen[key] = seen.get(key, 0) + 1
    reset |= {k for k, n in seen.items() if n >= config.RESET_MIN and now.get(k, 0) == 0}
    return reset


def held_places(snap, state, reset):
    """Find places of an issue whose synced values read empty with no sign of a clear.

    A board counts when its synced fields held at least ``config.WIPE_MIN``
    values in the state, not counting reset fields and places marked gone,
    and every synced field there reads empty now. The issue place counts when
    one such field reads empty without a removal time, since a clear of an
    issue field always has one. Such a place is treated as if it did not
    hold the issue in this run, so the empty read clears no other place; when
    it reads the same in the next run, it receives the agreed values.

    Parameters
    ----------
    snap : dict
        Snapshot of all places.
    state : dict
        State of the previous run.
    reset : set of tuple
        ``(place_key, fkey)`` pairs from ``reset_places``.

    Returns
    -------
    set of tuple
        ``(issue, place)`` pairs to hold back.
    """
    out, old = set(), state.get("items", {})
    for iid, item in snap["items"].items():
        for place in item["places"]:
            held = filled = lost = 0
            for fkey, spec in config.FIELDS.items():
                if place not in scope(snap, item, fkey):
                    continue
                value, at = raw_value(item, place, spec["places"][place])
                filled += value is not None
                rec = old.get(iid, {}).get(fkey, {})
                counted = (place_key(item, place), fkey) not in reset and place not in rec.get("gone", ())
                if counted and rec.get("seen", {}).get(place) is not None:
                    held += 1
                    lost += value is None and at is None
            if lost if place == "issue" else held >= config.WIPE_MIN and filled == 0:
                out.add((iid, place))
    return out


def _newest(places, times):
    """Pick the place with the newest change time; ties go by ``PLACE_ORDER``."""
    return max(places, key=lambda p: (times[p] or "", -config.PLACE_ORDER.index(p)))


def _pick(fkey, places, current, raw, times):
    """Pick the winning value among places that each claim a new value.

    A value that every claiming place already shows (after mapping to that
    place) wins without a conflict; ``Starter Projects`` on board 2 and
    ``TBD`` on board 1 agree. Otherwise the newest change wins.

    Returns
    -------
    tuple
        Winning canonical value, the conflict dict (empty if none), and the
        change time of the winner.
    """
    agreeing = [p for p in places if all(raw[q] == to_place(fkey, q, current[p]) for q in places)]
    if agreeing:
        win = _newest(agreeing, times)
        return current[win], {}, times[win]
    win = _newest(places, times)
    return current[win], {p: current[p] for p in places}, times[win]


def decide(fkey, places, current, raw, times, record):
    """Decide the canonical value of one field of one issue.

    Parameters
    ----------
    fkey : str
        Canonical field key.
    places : list of str
        Places in scope.
    current : dict
        Place to current canonical value.
    raw : dict
        Place to current value as the place names it.
    times : dict
        Place to ISO change time or ``None``.
    record : dict or None
        State record ``{"c": ..., "t": ..., "seen": {...}, "gone": [...]}``
        of the previous run, or ``None`` for an issue and field seen for the
        first time. ``t`` is the change time of the agreed value; ``gone``
        lists places that did not hold the issue or the field then.

    Returns
    -------
    tuple
        Canonical value, the conflicting values as a place-to-value dict
        (empty when there is no conflict), and the change time of the value.
    """
    if record is None:
        filled = [p for p in places if current[p] is not None]
        if not filled:
            return None, {}, None
        return _pick(fkey, filled, current, raw, times)
    seen, gone = record.get("seen", {}), set(record.get("gone", ()))
    changed = []
    for p in places:
        before = seen.get(p, _NEW)
        if p in gone and before is not _NEW:
            # A returning place: its old value is no change, and a new value or a clear counts
            # only when it is newer than the agreed value (of known time); a board clear has
            # no time, so an empty board place receives.
            newer = record.get("t") is not None and (times[p] or "") > record["t"]
            if current[p] != before and newer:
                changed.append(p)
        elif before is _NEW:
            # A place that joins with a value counts as a change; an empty one receives.
            if current[p] is not None and raw[p] != to_place(fkey, p, record.get("c")):
                changed.append(p)
        elif current[p] != before:
            changed.append(p)
    if not changed:
        return record.get("c"), {}, record.get("t")
    return _pick(fkey, changed, current, raw, times)


def plan(snap, state):
    """Compute all writes that bring every place to the agreed values.

    Parameters
    ----------
    snap : dict
        Snapshot of all places.
    state : dict
        State of the previous run (``{}`` on the first run).

    Returns
    -------
    Plan
        Writes, new state, conflicts and skipped values.

    Notes
    -----
    An issue that is on no board for ``config.GONE_DAYS`` days (counted from
    ``snap["read_at"]``) is dropped from the state.
    """
    result = Plan(state={"items": {}, "fields": field_ids(snap, state.get("fields"))})
    reset = reset_places(snap, state)
    result.resets = sorted(reset)
    held = held_places(snap, state, reset)
    result.held = sorted(held)
    if held:
        items = {iid: dict(item) for iid, item in snap["items"].items()}
        for iid, place in held:
            items[iid]["places"] = {p: v for p, v in items[iid]["places"].items() if p != place}
        snap = {**snap, "items": items}
    old = state.get("items", {})
    now = snap.get("read_at")
    for iid, entry in old.items():
        if iid not in snap["items"]:
            # Not readable or on no board this run: keep its records, every place gone.
            kept = {
                k: {**r, "gone": sorted(r.get("seen", {})), "gone_since": r.get("gone_since") or now}
                for k, r in entry.items()
            }
            since = min((r["gone_since"] for r in kept.values() if r["gone_since"]), default=None)
            if not (now and since and _days(since, now) > config.GONE_DAYS):
                result.state["items"][iid] = kept
    for iid, item in sorted(snap["items"].items()):
        new_entry = {}
        for fkey, spec in config.FIELDS.items():
            places = scope(snap, item, fkey)
            if not places:
                if fkey in old.get(iid, {}):
                    # The field is in no place: every place keeps its record and counts as gone.
                    r = old[iid][fkey]
                    new_entry[fkey] = {k: v for k, v in r.items() if k != "gone_since"} | {
                        "gone": sorted(r.get("seen", {}))
                    }
                continue
            raw, current, times = {}, {}, {}
            for p in places:
                raw[p], times[p] = raw_value(item, p, spec["places"][p])
                current[p] = to_canon(fkey, p, raw[p])
            record = old.get(iid, {}).get(fkey)
            if record is not None:
                record = {
                    "c": record.get("c"),
                    "seen": {
                        p: v for p, v in record.get("seen", {}).items() if (place_key(item, p), fkey) not in reset
                    },
                    "gone": record.get("gone", []),
                    "t": record.get("t"),
                }
            c, clash, t = decide(fkey, places, current, raw, times, record)
            if clash:
                result.conflicts.append({"issue": iid, "url": item["url"], "fkey": fkey, "values": clash, "chosen": c})
            seen = {}
            for p in places:
                target = to_place(fkey, p, c)
                seen[p] = current[p]
                if raw[p] == target:
                    continue
                fdef = place_fields(snap, item, p)[spec["places"][p]]
                if target is not None and spec["type"] == "select" and target not in fdef.get("options", {}):
                    result.skipped.append({"issue": iid, "url": item["url"], "place": p, "fkey": fkey, "value": target})
                    continue
                result.writes.append(
                    {
                        "issue": iid,
                        "url": item["url"],
                        "place": p,
                        "fkey": fkey,
                        "field": spec["places"][p],
                        "value": target,
                        "prev": current[p],
                    }
                )
                seen[p] = to_canon(fkey, p, target)
            # Places out of scope keep their record; those without the issue are marked gone.
            kept = {p: v for p, v in (record or {}).get("seen", {}).items() if p not in places}
            # A place stays gone until it holds the issue and the field again.
            gone = sorted(p for p in kept if p not in item["places"] or p in (record or {}).get("gone", ()))
            new_entry[fkey] = {"c": c, "t": t, "seen": {**kept, **seen}, "gone": gone}
        _derive(snap, item, iid, new_entry, result)
        if new_entry:
            result.state["items"][iid] = new_entry
    return result


def _days(start, end):
    """Days between two ISO times."""
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)) / timedelta(days=1)


def _derive(snap, item, iid, entry, result):
    """Add writes for derived fields such as Effort from Workload."""
    for spec in config.DERIVED.values():
        source = entry.get(spec["source"], {}).get("c")
        place = spec["place"]
        fdef = place_fields(snap, item, place).get(spec["field"]) if place in item["places"] else None
        if source is None or fdef is None:
            continue
        target = spec["map"].get(source)
        if target is None:
            continue
        if target not in fdef.get("options", {}):
            result.skipped.append(
                {"issue": iid, "url": item["url"], "place": place, "fkey": spec["field"], "value": target}
            )
            continue
        if raw_value(item, place, spec["field"])[0] != target:
            result.writes.append(
                {
                    "issue": iid,
                    "url": item["url"],
                    "place": place,
                    "fkey": None,
                    "field": spec["field"],
                    "value": target,
                    "prev": None,
                }
            )


def undo_failed(state, failed):
    """Restore the state records of writes that failed.

    Parameters
    ----------
    state : dict
        New state from ``plan``; changed in place.
    failed : list of dict
        Write entries that the API refused.

    Returns
    -------
    dict
        The same state object.
    """
    for w in failed:
        if w["fkey"] is None:
            continue
        rec = state["items"].get(w["issue"], {}).get(w["fkey"])
        if rec is not None:
            rec["seen"][w["place"]] = w["prev"]
    return state
