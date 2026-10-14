# board-sync

Keeps shared fields equal across 4 places:

- the org issue fields on each issue (Interra-Project and FormingWorlds),
- the Interra Management board (Interra-Project project 1),
- the Interra Collaboration board (Interra-Project project 2),
- the PROTEUS Development Roadmap (FormingWorlds project 7).

When a value changes in one place, the next run writes it to the others.

| Field | Issue field | Management | Collaboration | PROTEUS |
|---|---|---|---|---|
| Start date | yes | yes | yes | yes |
| Target date | yes | yes | yes | yes |
| Priority | yes (Urgent to Low) | yes | yes | yes |
| Workload | Effort, written from Workload | yes | yes | yes |
| Category, Sub-team, Release | | yes | yes | |
| Status | | yes | yes | |

The value names per place and the field list are in `board_sync/config.py`.
Status values `Import` and `Starter Projects` show as `TBD` on the Management board.
Effort follows Workload in one direction: a hand edit of Effort on an issue that has a Workload is overwritten, and a cleared Workload leaves Effort as it is.

## Rules

- The private repository `Interra-Project/board-sync-state` holds, per issue and field, the agreed value and the value last seen at each place.
  A place whose value differs from its last seen value changed since the previous run.
- When several places changed, the newest change wins.
  Board values and issue field values have a change time; a cleared issue field takes the time of its removal event.
  A board does not record when a value was cleared, so a cleared board value loses to a value set at another place in the same interval.
  The issue timeline read covers the last 100 field events; an older change has no time and loses.
- A value that every changed place already shows, after the name map, wins without a conflict.
- A cleared value is cleared in the other places too.
  A field that is emptied as a whole at one place (all of its 5 or more values, see below) is filled again from the others.
- A field that is missing at a place is skipped there, never read as empty, and the run log names it; a value without a matching option is listed in `report.txt` as skipped.
  A field with a new id (deleted and created again), or a field that held 5 or more values and now holds none, does not clear the other places.
- An issue that is not on a board, or cannot be read, keeps its record for that board.
  When it is there again with the field, its empty fields receive the agreed values, an unchanged value counts as no change, and a different value wins only when it is newer than the agreed value (an agreed value of unknown time keeps its place).
  On a field that was created again, the first value counts as a change, as on a new board.
  An issue that is on no board for 90 days is dropped from the state.
- A list longer than one page of the API (fields of a board, values of an item, issue fields, issue field values) stops the read with an error, so a cut-off list is never read as empty values.
- A run that plans more than 20 writes stops and writes nothing (exit code 3).
  Read the plan in `report.txt` in the state repository, then start the workflow by hand with `apply` and `allow_bulk`.
  The state then holds the approved writes that are not done yet, and the scheduled runs write them, at most 400 values per run.
  A later run stops again when its plan holds more than 20 new writes: writes outside the approved set that never failed before.
  The approval ends when all approved writes are done, or after 4 runs in a row in which no write succeeds and none reaches parking; a new hand run with `allow_bulk` starts it again.
- After 5 failed writes in a row, or 10 minutes after the run started, a run stops writing; failed and unwritten values are written again by the next run, the failed ones after all others.
  A write that failed in 3 runs is parked: it is listed in `report.txt` and tried again once a day, or at once when its value changes.
  Parked writes give a warning in the run, not a failed run.
- `report.txt` holds the plan before the first write, and the results after the last.
- Each run stores the raw read of all places (`snapshot.json`) and the full report (`report.txt`) in the state repository, so lost values can be restored from its history.
  The 3 files of a run replace the stored ones whole, and a run that changes nothing makes no commit.
- The job log of this public repository shows counts per board and field, field names and error types only.
  Issue links, values and error text stay in the state repository.
- A run reads the places one after the other and then writes.
  An edit made in the minutes of a run can be overwritten by that run; the next run then sees the written value as agreed.
- Exit codes: 0 done, 1 some writes failed, 2 the read, the state file, a token or the run failed, 3 too many changes, 4 done but some writes are parked (the workflow shows it as a warning), 143 or 130 stopped by a signal.

## Setup

1. Create a GitHub App that can be installed on any account (both orgs need it), with these permissions and no webhook:
   - Repository: Issues read and write, Metadata read.
   - Organization: Projects read and write, Issue fields read.
2. Install it on Interra-Project and on FormingWorlds, with access to all repositories whose issues are on the boards.
3. Protect `main` in this repository with a rule that requires a pull request with an approving review and the `test` check, and blocks force pushes and deletion, because code on `main` runs with the App key and the deploy key.
   Then create the environment `board-sync` and limit its deployment branches to `main`.
   Do not add a required reviewer: it would hold every scheduled run too.
4. Create `Interra-Project/board-sync-state` as a private repository with one commit on `main`, for example a README.
5. Store all 3 values in the environment only, not as repository secrets: the App client id as the variable `BOARD_SYNC_CLIENT_ID`, the App private key as the secret `BOARD_SYNC_PRIVATE_KEY`, and the private key of an SSH deploy key with write access to `Interra-Project/board-sync-state` as the secret `STATE_DEPLOY_KEY`.

The workflow runs at minute 7 and 37 of every hour on `main`; a run on another branch is skipped.
A run started by hand from the Actions tab only plans, unless `apply` is set.
GitHub turns off scheduled workflows in a public repository after 60 days without activity in that repository.
Check the date of the last run in the Actions tab of this repository, and enable the workflow again there when it is off.

## Local run

```bash
GH_TOKEN=$(gh auth token) python -m board_sync --state state.json --snapshot-dir snapshot
```

Without `--apply` the run changes nothing on GitHub; it writes only the snapshot and report files it is given.
These files hold private board data; `.gitignore` keeps them out of commits.
`scripts/store_state.sh` is the workflow step that stores the files of a run in the state repository.
`BOARD_SYNC_TOKEN_INTERRA_PROJECT` and `BOARD_SYNC_TOKEN_FORMINGWORLDS` set one token per org.

## Tests

```bash
python -m pytest
```
