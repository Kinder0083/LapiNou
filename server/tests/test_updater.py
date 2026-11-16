# SPDX-License-Identifier: MIT
"""Mise à jour du serveur : git pull, installer/post-update.sh, redémarrage (commandes simulées)."""

import subprocess
from pathlib import Path

import pytest

from lapinou.config import Settings, UpdateConfig
from lapinou.updater import Updater


class FakeRun:
    """Remplace subprocess.run : journalise et répond selon la commande."""

    def __init__(self, revs=("aaaa111", "bbbb222"), fail=None):
        self.calls: list[list[str]] = []
        self.revs = list(revs)
        self.fail = fail or set()

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        name = " ".join(cmd)
        out = ""
        if "rev-parse" in cmd:
            out = self.revs[0] if len(self.revs) == 1 else self.revs.pop(0)
            out += "\n"
        elif "pull" in cmd:
            out = "From https://x-access-token:ghp_SECRET@github.com/Kinder0083/LapiNou\n"
        rc = 1 if any(f in name for f in self.fail) else 0
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "installer").mkdir()
    (tmp_path / "installer" / "post-update.sh").write_text("#!/bin/bash\n")
    return tmp_path


async def test_update_pulls_runs_post_update_and_restarts(repo):
    run = FakeRun()
    up = Updater(repo, ["systemctl", "--no-block", "restart", "lapinou"], run=run,
                 restart_delay_s=0)
    result = await up.update()
    assert result.ok and result.restarting
    assert (result.before, result.after) == ("aaaa111", "bbbb222")
    cmds = [c for c in run.calls if "rev-parse" not in c]
    assert cmds[0] == ["git", "-C", str(repo), "pull", "--ff-only"]
    assert cmds[1] == ["bash", str(repo / "installer" / "post-update.sh")]
    await up.wait_restart()
    assert run.calls[-1] == ["systemctl", "--no-block", "restart", "lapinou"]
    assert "ghp_SECRET" not in result.output  # identifiants masqués dans la sortie


async def test_already_up_to_date_does_nothing_more(repo):
    run = FakeRun(revs=("aaaa111",))
    up = Updater(repo, ["true"], run=run, restart_delay_s=0)
    result = await up.update()
    assert result.ok and not result.restarting and result.message == "Déjà à jour."
    assert not any("post-update.sh" in " ".join(c) for c in run.calls)


async def test_failed_pull_stops_before_restart(repo):
    run = FakeRun(fail={"pull"})
    up = Updater(repo, ["systemctl", "restart", "lapinou"], run=run, restart_delay_s=0)
    result = await up.update()
    assert not result.ok and not result.restarting
    assert "git pull" in result.message
    assert not any(c[0] in ("bash", "systemctl") for c in run.calls)


async def test_failed_post_update_does_not_restart(repo):
    run = FakeRun(fail={"post-update.sh"})
    up = Updater(repo, ["systemctl", "restart", "lapinou"], run=run, restart_delay_s=0)
    result = await up.update()
    assert not result.ok and not result.restarting
    assert "post-update.sh" in result.message
    assert not any(c[0] == "systemctl" for c in run.calls)


async def test_missing_post_update_is_skipped(tmp_path):
    run = FakeRun()
    up = Updater(tmp_path, ["systemctl", "restart", "lapinou"], run=run, restart_delay_s=0)
    result = await up.update()
    assert result.ok and not any(c[0] == "bash" for c in run.calls)


def test_from_settings_defaults_to_this_clone():
    up = Updater.from_settings(Settings())
    assert up.repo_dir == Path(__file__).resolve().parents[2]
    assert up.restart_command == ["systemctl", "--no-block", "restart", "lapinou"]
    up = Updater.from_settings(Settings(update=UpdateConfig(repo_dir="/opt/lapinou",
                                                            restart_command=["x"])))
    assert up.repo_dir == Path("/opt/lapinou") and up.restart_command == ["x"]
