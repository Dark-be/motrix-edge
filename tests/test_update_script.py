# Confidential Information of Motphys. Not for disclosure or distribution without Motphys's prior
# written consent.
#
# This software contains code, techniques and know-how which is confidential and proprietary to
# Motphys.
#
# Product and Trade Secret source code contains trade secrets of Motphys.
#
# Copyright (C) 2020-2026 Motphys Technology Co., Ltd. All Rights Reserved.
#
# This software belongs to the Intellectual Property of Motphys. Use of this software is subject to
# the terms and conditions in the license file accompanying. You may not use this software except
# in compliance with the license file.

"""Edge 本机安全更新脚本测试。"""

import subprocess
from pathlib import Path
from shutil import copy2

SCRIPT = Path(__file__).parents[1] / "scripts" / "update.sh"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, content: str, message: str) -> None:
    (repo / "version.txt").write_text(content, encoding="utf-8")
    _git("add", "version.txt", cwd=repo)
    _git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message, cwd=repo)


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    edge = tmp_path / "edge"
    _git("init", "--bare", str(remote))
    _git("init", "-b", "dev", str(seed))
    (seed / "scripts").mkdir()
    copy2(SCRIPT, seed / "scripts" / "update.sh")
    _git("add", "scripts/update.sh", cwd=seed)
    _commit(seed, "one\n", "one")
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "dev", cwd=seed)
    _git("clone", "-b", "dev", str(remote), str(edge))
    return seed, edge


def test_update_script_fast_forwards_clean_checkout(tmp_path):
    seed, edge = _repos(tmp_path)
    before = _git("rev-parse", "HEAD", cwd=edge)
    _commit(seed, "two\n", "two")
    _git("push", "origin", "dev", cwd=seed)

    result = subprocess.run(["bash", "scripts/update.sh"], cwd=edge, check=True, capture_output=True, text=True)

    after = _git("rev-parse", "HEAD", cwd=edge)
    assert before != after
    assert "更新完成" in result.stdout


def test_update_script_refuses_dirty_checkout(tmp_path):
    seed, edge = _repos(tmp_path)
    _commit(seed, "two\n", "two")
    _git("push", "origin", "dev", cwd=seed)
    (edge / "local.txt").write_text("local\n", encoding="utf-8")

    result = subprocess.run(["bash", "scripts/update.sh"], cwd=edge, check=False, capture_output=True, text=True)

    assert result.returncode == 3
    assert "本地修改" in result.stderr
