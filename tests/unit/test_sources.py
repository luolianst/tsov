"""S4.1 全局音源库：稳定 id / 增量扫描 / 缺失 / relink / 搜索。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tsov.sources import SourceError, SourceLibrary
from tsov.core.score import Instrument, Score, Track
from tsov.host.project import Project
from tsov.host.command import EditBatch


def _vst3(root: Path, name: str = "Fake.vst3", cid: str = "ABCDEF0123456789") -> Path:
    bundle = root / name
    res = bundle / "Contents" / "Resources"
    res.mkdir(parents=True)
    # 真实 VST3 moduleinfo 常见尾随逗号；解析器只取 CID/Name，不要求严格 JSON
    (res / "moduleinfo.json").write_text(
        '{\n  "Name": "Fake Synth",\n  "Classes": [\n'
        '    {"CID": "' + cid + '", "Category": "Audio Module Class", "Name": "Fake Synth",},\n'
        '  ],\n}\n',
        encoding="utf-8",
    )
    return bundle


def _tree(root: Path) -> None:
    _vst3(root / "vst3")
    (root / "sfz").mkdir()
    (root / "sfz" / "piano.sfz").write_text("<region> sample=a.wav key=60\n", encoding="utf-8")
    (root / "sf2").mkdir()
    (root / "sf2" / "kit.sf2").write_bytes(b"fake-sf2")


def test_scan_stable_ids_and_summary(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    _tree(root)
    lib = SourceLibrary(tmp_path / "sources.json", roots=[str(root)])
    summary = lib.scan()

    assert summary["added"] == 3
    assert summary["missing"] == 0
    assert summary["total"] == 3
    ids = {s.id for s in lib.sources.values()}
    assert "vst3:abcdef0123456789" in ids
    assert any(x.startswith("sfz:") for x in ids)
    assert any(x.startswith("sf2:") for x in ids)
    assert (tmp_path / "sources.json").is_file()

    # 重新加载后 id 稳定
    again = SourceLibrary(tmp_path / "sources.json")
    assert {s.id for s in again.sources.values()} == ids


def test_duplicate_sfz_prefers_complete_sample_copy(tmp_path):
    """同内容 SFZ 多副本：优先保留采样文件找得到的目录（VPO extracted vs 完整包）。"""
    root = tmp_path / "sources"
    broken = root / "a-broken"
    full = root / "z-full"
    broken.mkdir(parents=True)
    full.mkdir(parents=True)
    text = "<region> sample=hit.wav key=60\n"
    (broken / "same.sfz").write_text(text, encoding="utf-8")
    (full / "same.sfz").write_text(text, encoding="utf-8")
    (full / "hit.wav").write_bytes(b"RIFF-fake")
    lib = SourceLibrary(tmp_path / "sources.json", roots=[str(root)])
    lib.scan()
    sfz = next(s for s in lib.sources.values() if s.kind == "sfz")
    assert Path(sfz.path) == (full / "same.sfz").resolve()
    assert any("采样更完整" in e for e in lib.errors)


def test_rescan_after_move_keeps_content_id(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    _tree(root)
    lib = SourceLibrary(tmp_path / "sources.json", roots=[str(root)])
    lib.scan()
    sfz = next(s for s in lib.sources.values() if s.kind == "sfz")
    sid = sfz.id
    moved = root / "sfz" / "renamed.sfz"
    (root / "sfz" / "piano.sfz").rename(moved)

    summary = lib.scan()
    assert summary["added"] == 0
    assert summary["total"] == 3
    got = lib.get(sid)
    assert Path(got.path) == moved.resolve()
    assert got.missing is False


def test_missing_and_relink_content_guard(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    _tree(root)
    lib = SourceLibrary(tmp_path / "sources.json", roots=[str(root)])
    lib.scan()
    sfz = next(s for s in lib.sources.values() if s.kind == "sfz")
    sid, original = sfz.id, Path(sfz.path)
    original.unlink()
    summary = lib.scan()
    assert summary["missing"] == 1
    assert lib.get(sid).missing is True

    wrong = root / "sfz" / "wrong.sfz"
    wrong.write_text("<region> sample=b.wav key=60\n", encoding="utf-8")
    with pytest.raises(SourceError, match="哈希不符"):
        lib.relink(sid, wrong)

    original.write_text("<region> sample=a.wav key=60\n", encoding="utf-8")
    fixed = lib.relink(sid, original)
    assert fixed.missing is False
    assert fixed.content_hash


def test_vst3_relink_rejects_class_mismatch(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    bundle = _vst3(root)
    lib = SourceLibrary(tmp_path / "sources.json", roots=[str(root)])
    lib.scan()
    src = next(s for s in lib.sources.values() if s.kind == "vst3")
    other = _vst3(root / "other", name="Other.vst3", cid="FFFF0000")
    with pytest.raises(SourceError, match="身份不符"):
        lib.relink(src.id, other)


def test_search_favorite_recent(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    _tree(root)
    lib = SourceLibrary(tmp_path / "sources.json", roots=[str(root)])
    lib.scan()
    sfz = next(s for s in lib.sources.values() if s.kind == "sfz")
    lib.set_favorite(sfz.id, True)
    lib.mark_recent(sfz.id)
    lib.save()

    again = SourceLibrary(tmp_path / "sources.json")
    rows = again.search("piano")
    assert rows and rows[0].id == sfz.id
    assert rows[0].favorite is True
    assert again.search("", kind="vst3")[0].kind == "vst3"
    assert again.search("", favorites_only=True)[0].id == sfz.id


def test_mount_source_command(tmp_path, monkeypatch):
    root = tmp_path / "sources"
    root.mkdir()
    _tree(root)
    lib = SourceLibrary(tmp_path / "sources.json", roots=[str(root)])
    lib.scan()
    monkeypatch.setenv("TSOV_SOURCES_PATH", str(tmp_path / "sources.json"))
    vst = next(s for s in lib.sources.values() if s.kind == "vst3")
    sfz = next(s for s in lib.sources.values() if s.kind == "sfz")

    score = Score(title="mount", tracks=[Track(name="lead", instrument=Instrument(program="piano"))])
    project = Project.create("mount", score, parent=tmp_path)
    batch = EditBatch(label="mount").add("mount_source", track=0,
                                         value={"source_id": vst.id})
    result = project.apply_batch(batch)
    assert result["applied"] == 1 and not result["errors"], result
    inst = project.score.tracks[0].instrument
    assert inst.program == f"vst3:{vst.path}"
    assert inst.source_id == vst.id

    batch = EditBatch(label="mount-sfz").add(
        "mount_source", track=0, value={"source_id": sfz.id})
    result = project.apply_batch(batch)
    assert result["applied"] == 1 and not result["errors"], result
    assert project.score.tracks[0].instrument.program == f"sfz:{sfz.path}"
    assert project.score.tracks[0].instrument.source_id == sfz.id
