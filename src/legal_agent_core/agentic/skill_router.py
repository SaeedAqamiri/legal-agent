"""Persian procedural skills injected into the planner system prompt.

Skills are GUIDANCE ONLY — they never authorize a tool. The tool surface is
fixed by ``LegalResearchTools``; the router only picks a starting recipe and
the planner may deviate freely.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

_SKILLS_PACKAGE = "legal_agent_core.agentic.skills"


@dataclass(frozen=True, slots=True)
class Skill:
    skill_id: str
    keywords: tuple[str, ...]
    body: str

    def render(self) -> str:
        return f"### مهارت: {self.skill_id}\n\n{self.body.strip()}"


def _load(skill_id: str) -> str:
    return (
        resources.files(_SKILLS_PACKAGE)
        .joinpath(f"{skill_id}.md")
        .read_text(encoding="utf-8")
    )


_SKILL_DEFS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "deadline_appeal",
        ("مهلت", "اعتراض", "تجدیدنظر", "فرجه", "ددیه", "پلاک", "ابلاغ"),
    ),
    (
        "temporal_version",
        ("نسخه", "تاریخ", "قبلاً", "قبلا", "سابق", "مصوب", "اصلاحی", "الحاقی", "منسوخ"),
    ),
    (
        "cross_reference",
        ("ارجاع", "موضوع ماده", "ماده مذکور", "همان ماده", "طبق ماده", "بند ماده"),
    ),
    (
        "navigate_document",
        ("فهرست", "صفحه", "کدام صفحه", "در چه صفحه", "بخش", "فصل", "کجا", "داخل سند"),
    ),
)


def all_skills() -> tuple[Skill, ...]:
    return tuple(
        Skill(skill_id, keywords, _load(skill_id)) for skill_id, keywords in _SKILL_DEFS
    )


def select_skill(question: str) -> Skill | None:
    """Keyword router: ONE starting skill per question; testable and simple."""
    lowered = question.casefold()
    best: tuple[int, Skill] | None = None
    for skill in all_skills():
        hits = sum(1 for keyword in skill.keywords if keyword.casefold() in lowered)
        if hits <= 0:
            continue
        if best is None or hits > best[0]:
            best = (hits, skill)
    return best[1] if best else None


def skills_index() -> str:
    lines = ["مهارت‌های موجود (پیشنهاد رویه‌اند، نه الزام):"]
    for skill in all_skills():
        lines.append(f"- {skill.skill_id}")
    return "\n".join(lines)


def augment_system_prompt(system_prompt: str, question: str) -> str:
    skill = select_skill(question)
    if skill is None:
        return system_prompt
    return f"{system_prompt}\n\n{skill.render()}"
