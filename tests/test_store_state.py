"""Tests of the script that stores a run's files in the state repository, against local git repositories."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "store_state.sh"
pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash")

IDENTITY = {
    "GIT_AUTHOR_NAME": "board-sync",
    "GIT_AUTHOR_EMAIL": "board-sync@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "board-sync",
    "GIT_COMMITTER_EMAIL": "board-sync@users.noreply.github.com",
}


def git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repos(tmp_path):
    """A bare remote with one README commit, a run checkout, and a clean environment without a git identity."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "TMPDIR": str(tmp_path), "STORE_RETRY_WAIT": "0", **IDENTITY}
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote), env=env)
    seed = tmp_path / "seed"
    git(tmp_path, "clone", "-q", str(remote), str(seed), env=env)
    (seed / "README.md").write_text("readme\n")
    git(seed, "add", ".", env=env)
    git(seed, "commit", "-q", "-m", "init", env=env)
    git(seed, "push", "-q", "origin", "HEAD:main", env=env)
    run = tmp_path / "state"
    git(tmp_path, "clone", "-q", "-b", "main", str(remote), str(run), env=env)
    return {"tmp": tmp_path, "env": env, "remote": remote, "seed": seed, "run": run}


def store(r, run_id="1"):
    return subprocess.run(
        ["bash", str(SCRIPT), str(r["run"]), run_id], env=r["env"], capture_output=True, text=True, check=False
    )


def remote_files(r):
    check = r["tmp"] / f"check{len(list(r['tmp'].glob('check*')))}"
    git(r["tmp"], "clone", "-q", "-b", "main", str(r["remote"]), str(check), env=r["env"])
    return {p.name: p.read_text() for p in check.iterdir() if p.is_file()}, git(
        check, "log", "--format=%s", env=r["env"]
    )


def test_first_run_without_a_state_file_stores_snapshot_and_report(repos):
    (repos["run"] / "snapshot.json").write_text("{}\n")
    (repos["run"] / "report.txt").write_text("stop: 30 writes\n")
    assert store(repos).returncode == 0
    files, log = remote_files(repos)
    assert files["report.txt"] == "stop: 30 writes\n" and "snapshot.json" in files and "state.json" not in files
    assert log.splitlines()[0] == "Sync run 1"


def test_run_files_replace_a_changed_remote_whole_and_keep_other_files(repos):
    (repos["seed"] / "state.json").write_text('{"v": "human"}\n')
    (repos["seed"] / "README.md").write_text("readme 2\n")
    git(repos["seed"], "add", ".", env=repos["env"])
    git(repos["seed"], "commit", "-q", "-m", "human", env=repos["env"])
    git(repos["seed"], "push", "-q", "origin", "HEAD:main", env=repos["env"])
    (repos["run"] / "state.json").write_text('{"v": "run"}\n')
    (repos["run"] / "state.json.tmp").write_text("partial")
    assert store(repos, "7").returncode == 0
    files, log = remote_files(repos)
    assert files["state.json"] == '{"v": "run"}\n' and files["README.md"] == "readme 2\n"
    assert "state.json.tmp" not in files and log.splitlines()[:2] == ["Sync run 7", "human"]


def test_nothing_changed_makes_no_commit(repos):
    (repos["run"] / "report.txt").write_text("issues: 1\n")
    assert store(repos).returncode == 0
    second = store(repos, "2")
    assert second.returncode == 0 and "state unchanged" in second.stdout
    assert len(remote_files(repos)[1].splitlines()) == 2


def test_missing_checkout_is_not_an_error(repos):
    result = subprocess.run(["bash", str(SCRIPT), str(repos["tmp"] / "absent"), "1"], env=repos["env"], check=False)
    assert result.returncode == 0


def test_push_failure_is_reported(repos):
    (repos["run"] / "report.txt").write_text("x\n")
    git(repos["run"], "remote", "set-url", "origin", str(repos["tmp"] / "nowhere.git"), env=repos["env"])
    result = store(repos)
    assert result.returncode == 1 and "state push failed after 3 attempts" in result.stdout


def test_rejected_push_is_tried_again(repos):
    hook = repos["remote"] / "hooks" / "pre-receive"
    count = repos["tmp"] / "pushes"
    hook.write_text(
        f'#!/bin/sh\nn=$(cat "{count}" 2>/dev/null || echo 0)\necho $((n + 1)) > "{count}"\n[ "$n" -ge 1 ]\n'
    )
    hook.chmod(0o755)
    (repos["run"] / "report.txt").write_text("x\n")
    result = store(repos)
    assert result.returncode == 0 and count.read_text().strip() == "2"
    files, log = remote_files(repos)
    assert files["report.txt"] == "x\n" and log.splitlines()[0] == "Sync run 1"


def test_failed_commit_is_reported(repos):
    hook = repos["run"] / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (repos["run"] / "report.txt").write_text("x\n")
    result = store(repos)
    assert result.returncode == 1 and "cannot commit" in result.stdout
    assert "report.txt" not in remote_files(repos)[0]


def test_a_folder_that_is_not_a_repository_is_left_alone(repos):
    plain = repos["tmp"] / "plain"
    plain.mkdir()
    (plain / "report.txt").write_text("x\n")
    result = subprocess.run(
        ["bash", str(SCRIPT), str(plain), "1"], env=repos["env"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and "no state checkout" in result.stdout
    assert (plain / "report.txt").read_text() == "x\n"
