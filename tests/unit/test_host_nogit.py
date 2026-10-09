"""git 缺席优雅降级回归（2026-10-09 打包器审计——与 ffprobe 同族病：运行时外部件未配对）。

一键包零安装目标机可能没有 git：新建/打开工程不许崩（曾裸抛 WinError 2），
版本树/回滚/收藏应静默停用；工程创建/编辑/保存不受影响。
第二形态：有 .git 仓库但无 git 二进制（拷贝工程场景）同样不许崩。
"""

from __future__ import annotations

import shutil

import pytest

from tsov.core.score import Score, Track
from tsov.host import project as project_mod
from tsov.host.project import Project


def _score() -> Score:
    return Score(title="t", tempo=120.0, tracks=[Track(name="melody", notes=[])])


def _no_git(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("[WinError 2] 系统找不到指定的文件。")

    monkeypatch.setattr(project_mod.subprocess, "run", boom)


def test_project_create_without_git(tmp_path, monkeypatch):
    """无 git 机器：建工程/存盘/版本 API 全链不崩，优雅降级。"""
    _no_git(monkeypatch)
    p = Project.create("nogit", _score(), parent=tmp_path)
    assert (p.root / "score.json").is_file()
    assert (p.root / ".gitignore").is_file()
    assert p.log() == []                 # 版本树空
    assert p.commit("x") is None         # 提交静默失败
    assert p.score_at("HEAD") is None
    assert p.rollback() is False
    assert p.delete_favorite("fav/x") is False


@pytest.mark.skipif(shutil.which("git") is None, reason="setup 需要真 git 建仓库")
def test_repo_present_but_git_binary_missing(tmp_path, monkeypatch):
    """第二形态：.git 在、git 二进制没了 → 打开/commit/rollback 不崩。"""
    p0 = Project.create("hasgit", _score(), parent=tmp_path)
    assert p0._has_git()
    _no_git(monkeypatch)
    p = Project.open(p0.root)            # 不许抛
    assert p.commit("y") is None
    assert p.log() == []
    assert p.rollback() is False
