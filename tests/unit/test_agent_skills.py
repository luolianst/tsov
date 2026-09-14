"""skill 机制（SKILL.md 模式）单元测试：目录扫描 / 加载 / use_skill / 系统提示注入。

约定：不用 pytest tmp_path（handoff 坑 81）——手动 output/<uuid> 目录 + teardown rmtree。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from tsov.agent.prompt import SYSTEM_PROMPT, build_system_prompt
from tsov.agent.skills import SkillLibrary, tool_use_skill
from tsov.agent.tools import build_default_registry


def test_builtin_skills_exist():
    """内置技能目录：三个技能可列出 + 目录文本含名称与描述。"""
    lib = SkillLibrary()
    names = lib.names()
    assert "melody-edit" in names
    assert "transcription-diagnosis" in names
    assert "from-scratch-compose" in names
    catalog = lib.catalog()
    assert "melody-edit" in catalog and "：" in catalog
    # 正文可加载
    s = lib.get("melody-edit")
    assert s is not None and "transpose" in s.load()


def test_library_scans_custom_dir_and_loads():
    d = Path("output") / f"skilltest-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        (d / "a.md").write_text("---\nname: demo-a\ndescription: 演示技能 A\n---\n\n# 正文 A\n步骤 1\n", encoding="utf-8")
        (d / "b.md").write_text("没有 frontmatter 的技能\n第二行\n", encoding="utf-8")
        lib = SkillLibrary(dirs=[d])
        assert lib.names() == ["b", "demo-a"]
        assert "正文 A" in lib.get("demo-a").load()
        assert "演示技能 A" in lib.catalog()
        # 无 frontmatter：name=文件名、description=正文首行
        assert lib.get("b").description == "没有 frontmatter 的技能"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_use_skill_handler():
    lib = SkillLibrary()
    h = tool_use_skill(lib)
    out = h({"name": "melody-edit"})
    assert "【技能 melody-edit】" in out and "transpose" in out
    bad = h({"name": "no-such-skill"})
    assert "未知技能" in bad and "melody-edit" in bad  # 错误里给出可选清单


def test_system_prompt_includes_catalog():
    lib = SkillLibrary()
    p = build_system_prompt(lib)
    assert p.startswith(SYSTEM_PROMPT)
    assert "use_skill" in p and "melody-edit" in p
    # 空库：不加技能段
    empty = SkillLibrary(dirs=[])
    assert build_system_prompt(empty) == SYSTEM_PROMPT


def test_registry_registers_use_skill():
    reg = build_default_registry()
    names = [s.name for s in reg.specs()]
    assert "use_skill" in names
    tool = reg.get("use_skill")
    assert tool.handler({"name": "from-scratch-compose"}).startswith("【技能 from-scratch-compose】")
