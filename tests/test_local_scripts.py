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

"""本机启动 / 安全更新脚本的精简端到端测试。"""

import os
import subprocess
from pathlib import Path
from shutil import copy2

import pytest

ROOT = Path(__file__).parents[1]
START = ROOT / "scripts" / "start.sh"
UPDATE = ROOT / "scripts" / "update.sh"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, content: str) -> None:
    (repo / "version.txt").write_text(content, encoding="utf-8")
    _git("add", "version.txt", cwd=repo)
    _git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", content.strip(), cwd=repo)


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    remote, seed, edge = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "edge"
    _git("init", "--bare", str(remote), cwd=tmp_path)
    _git("init", "-b", "dev", str(seed), cwd=tmp_path)
    (seed / "scripts").mkdir()
    copy2(UPDATE, seed / "scripts" / "update.sh")
    _git("add", "scripts/update.sh", cwd=seed)
    _commit(seed, "one\n")
    _git("remote", "add", "origin", str(remote), cwd=seed)
    _git("push", "-u", "origin", "dev", cwd=seed)
    _git("clone", "-b", "dev", str(remote), str(edge), cwd=tmp_path)
    return seed, edge


def test_start_script_env_modes_and_argument_forwarding(tmp_path):
    repo, bin_dir = tmp_path / "repo", tmp_path / "bin"
    (repo / "scripts").mkdir(parents=True)
    bin_dir.mkdir()
    copy2(START, repo / "scripts" / "start.sh")
    args_file = tmp_path / "uv-args"
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/bash\nprintf \'%s\\n\' "$@" > "$UV_ARGS_FILE"\n', encoding="utf-8")
    uv.chmod(0o755)
    env = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}", "UV_ARGS_FILE": str(args_file)}
    env.pop("MOTRIX_EDGE_ENV_FILE", None)

    env_file = repo / ".env"
    env_file.write_text("MOTRIX_EDGE_DIR=/tmp/edge\n", encoding="utf-8")
    subprocess.run(["bash", "scripts/start.sh", "--config", "config with spaces.yml"], cwd=repo, env=env, check=True)
    assert args_file.read_text().splitlines() == [
        "run",
        "--env-file",
        str(env_file),
        "motrix-edge",
        "run",
        "--config",
        "config with spaces.yml",
    ]

    env_file.unlink()
    subprocess.run(["bash", "scripts/start.sh"], cwd=repo, env=env, check=True)
    assert args_file.read_text().splitlines() == ["run", "motrix-edge", "run"]

    missing_env = env | {"MOTRIX_EDGE_ENV_FILE": str(repo / "missing.env")}
    result = subprocess.run(["bash", "scripts/start.sh"], cwd=repo, env=missing_env, check=False)
    assert result.returncode == 1


def test_update_script_check_then_fast_forward(tmp_path):
    seed, edge = _repos(tmp_path)
    before = _git("rev-parse", "HEAD", cwd=edge)
    _commit(seed, "two\n")
    _git("push", "origin", "dev", cwd=seed)
    expected = _git("rev-parse", "HEAD", cwd=seed)

    subprocess.run(["bash", "scripts/update.sh", "--check"], cwd=edge, check=True)
    assert _git("rev-parse", "HEAD", cwd=edge) == before
    subprocess.run(["bash", "scripts/update.sh"], cwd=edge, check=True)
    assert _git("rev-parse", "HEAD", cwd=edge) == expected


@pytest.mark.parametrize("unsafe", ["dirty", "branch", "ahead"])
def test_update_script_rejects_unsafe_checkout(tmp_path, unsafe):
    _, edge = _repos(tmp_path)
    if unsafe == "dirty":
        (edge / "local.txt").write_text("local\n", encoding="utf-8")
    elif unsafe == "branch":
        _git("switch", "-c", "local-work", cwd=edge)
    else:
        _commit(edge, "local\n")
    before = _git("rev-parse", "HEAD", cwd=edge)

    for args in ([], ["--check"]):
        result = subprocess.run(["bash", "scripts/update.sh", *args], cwd=edge, check=False)

        assert result.returncode == 3
        assert _git("rev-parse", "HEAD", cwd=edge) == before


def test_update_script_help_and_extra_arguments():
    assert subprocess.run(["bash", str(UPDATE), "--help"], check=False).returncode == 0
    assert subprocess.run(["bash", str(UPDATE), "--check", "extra"], check=False).returncode == 2
