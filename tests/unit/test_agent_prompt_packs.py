"""系统提示词知识包注入（TSOV_KNOWLEDGE_PACKS）：注入「读卡」动作、不注入内容。"""

from tsov.agent.prompt import SYSTEM_PROMPT, build_system_prompt


def test_packs_env_appends_hint(monkeypatch):
    monkeypatch.setenv("TSOV_KNOWLEDGE_PACKS", "knowledge/a.md, knowledge/b.md")
    p = build_system_prompt(None)
    assert p.startswith(SYSTEM_PROMPT)
    assert "knowledge/a.md" in p and "knowledge/b.md" in p
    assert "read_text" in p


def test_packs_env_empty(monkeypatch):
    monkeypatch.delenv("TSOV_KNOWLEDGE_PACKS", raising=False)
    assert build_system_prompt(None) == SYSTEM_PROMPT


def test_packs_with_skills_catalog(monkeypatch, tmp_path):
    monkeypatch.delenv("TSOV_KNOWLEDGE_PACKS", raising=False)
    from tsov.agent.skills import SkillLibrary

    d = tmp_path / "skills"
    d.mkdir()
    (d / "x.md").write_text("---\nname: x\ndescription: 测试技能\n---\n正文", encoding="utf-8")
    lib = SkillLibrary(dirs=[str(d)])
    p = build_system_prompt(lib)
    assert p.startswith(SYSTEM_PROMPT)
    assert "测试技能" in p


def test_packs_env_with_skills(monkeypatch, tmp_path):
    monkeypatch.setenv("TSOV_KNOWLEDGE_PACKS", "knowledge/pack.md")
    from tsov.agent.skills import SkillLibrary

    d = tmp_path / "skills"
    d.mkdir()
    (d / "y.md").write_text("---\nname: y\ndescription: 技能乙\n---\n正文", encoding="utf-8")
    lib = SkillLibrary(dirs=[str(d)])
    p = build_system_prompt(lib)
    assert "knowledge/pack.md" in p and "技能乙" in p
