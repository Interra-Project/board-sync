"""Places and field maps for the board sync.

A *place* is one location that holds a value for an issue: the org issue
fields on the issue itself (``issue``) or one of the project boards
(``b1``, ``b2``, ``b7``). Canonical values use the vocabulary of the
Interra Collaboration board (``b2``).
"""

BOARDS = {
    "b1": {"owner": "Interra-Project", "number": 1, "title": "Interra Management"},
    "b2": {"owner": "Interra-Project", "number": 2, "title": "Interra Collaboration"},
    "b7": {"owner": "FormingWorlds", "number": 7, "title": "PROTEUS Development Roadmap"},
}

# Orgs whose org issue fields take part in the sync.
ISSUE_ORGS = ("Interra-Project", "FormingWorlds")

# Tie-break when two places changed at the same second.
PLACE_ORDER = ("issue", "b2", "b1", "b7")

PRIORITY = (
    "Priority 1: critical",
    "Priority 2: high",
    "Priority 3: standard",
    "Priority 4: low",
)

# Per canonical field: the field name at each place, and the value map
# from canonical value to place value where the two differ.
FIELDS = {
    "start": {
        "type": "date",
        "places": {"issue": "Start date", "b1": "Start date", "b2": "Start date", "b7": "Start date"},
    },
    "target": {
        "type": "date",
        "places": {"issue": "Target date", "b1": "Target date", "b2": "Target date", "b7": "Target date"},
    },
    "priority": {
        "type": "select",
        "places": {"issue": "Priority", "b1": "Priority", "b2": "Priority", "b7": "Priority"},
        "to_place": {
            "issue": dict(zip(PRIORITY, ("Urgent", "High", "Medium", "Low"))),
            "b1": dict(zip(PRIORITY, ("Priority: critical", "Priority: high", "Priority: normal", "Priority: low"))),
        },
    },
    "workload": {
        "type": "select",
        "places": {"b1": "Workload", "b2": "Workload", "b7": "Workload"},
    },
    "category": {"type": "select", "places": {"b1": "Category", "b2": "Category"}},
    "subteam": {
        "type": "select",
        "places": {"b1": "Sub-team", "b2": "Sub-team"},
        "to_place": {"b1": {"Co-leads": "Co-Leads"}},
    },
    "release": {"type": "select", "places": {"b1": "Release", "b2": "Release"}},
    "status": {
        "type": "select",
        "places": {"b1": "Status", "b2": "Status"},
        "to_place": {"b1": {"Import": "TBD", "Starter Projects": "TBD"}},
    },
}

# Fields written from another field and never read as a source.
DERIVED = {
    "effort": {
        "source": "workload",
        "place": "issue",
        "field": "Effort",
        "map": {"XL": "High", "L": "High", "M": "Medium", "S": "Low", "XS": "Low"},
    },
}

# A place whose field held at least this many values and now holds none is
# treated as a new, empty field, not as a mass clear.
RESET_MIN = 5

# An issue that is on no board for this many days is dropped from the state.
GONE_DAYS = 90
