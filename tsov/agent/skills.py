"""skill 机制（SKILL.md 模式，ADR-0012 里程碑二）：渐进披露的技能库。

- 技能 = 一个 markdown 文件（YAML 头部 + 正文）：name/description 进系统提示的「技能目录」，
  正文由 agent 调用 `use_skill` 工具按需加载（渐进披露——目录常驻、正文不撑上下文）
- 扫描目录：内置 `tsov/agent/skills/` + 可选的额外目录（构造参数）；工作目录下 `skills/` 存在时也扫
- 解析容错：无 frontmatter 时 name 取文件名、description 取正文首行
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BUILTIN_SKILLS_DIR = Path(__file__).parent / "skills"


@dataclass
class Skill:
    name: str
    description: str
    path: Path

    def load(self) -> str:
        """读技能全文（含 frontmatter；LLM 直接阅读）。"""
        return self.path.read_text(encoding="utf-8")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """极简 YAML 头部解析（只要 name/description 两键；不引 yaml 依赖）。"""
    meta: dict[str, str] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            head, body = parts[1], parts[2]
            for line in head.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
    return meta, body.strip()


def default_skill_dirs() -> list[Path]:
    """内置目录 + 工作目录 `skills/`（存在才加）。"""
    dirs = [BUILTIN_SKILLS_DIR]
    extra = Path.cwd() / "skills"
    if extra.is_dir():
        dirs.append(extra)
    return dirs


class SkillLibrary:
    """技能库：扫描目录 → 目录条目（catalog）+ 按名加载（get/load）。"""

    def __init__(self, dirs: list[str | Path] | None = None):
        self.dirs = [Path(d) for d in (dirs if dirs is not None else default_skill_dirs())]
        self._skills: dict[str, Skill] = {}
        self._scan()

    def _scan(self) -> None:
        self._skills.clear()
        for d in self.dirs:
            if not Path(d).is_dir():
                continue
            for p in sorted(Path(d).glob("*.md")):
                try:
                    text = p.read_text(encoding="utf-8")
                except OSError:  # 读不了就跳过（不阻塞 agent 启动）
                    continue
                meta, body = _parse_frontmatter(text)
                name = (meta.get("name") or p.stem).strip()
                desc = (meta.get("description") or (body.splitlines()[0] if body else "")).strip()
                self._skills[name] = Skill(name=name, description=desc, path=p)

    def names(self) -> list[str]:
        return sorted(self._skills)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def catalog(self) -> str:
        """系统提示用技能目录（「- 名称：一行描述」）。空库返回空串。"""
        if not self._skills:
            return ""
        return "\n".join(f"- {self._skills[n].name}：{self._skills[n].description}" for n in self.names())


def tool_use_skill(library: SkillLibrary):
    """构造 use_skill 工具处理器（闭包绑定技能库）。"""

    def handler(args: dict) -> str:
        name = str(args.get("name", "")).strip()
        skill = library.get(name)
        if skill is None:
            return f"未知技能：{name!r}（可选：{', '.join(library.names()) or '（空）'}）"
        return f"【技能 {skill.name}】\n{skill.load()}"

    return handler
