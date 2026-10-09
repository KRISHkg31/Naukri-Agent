"""Config loading, validation and the day-to-day rotation state."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

HEADLINE_MAX = 250
KEY_SKILLS_CHARS = 250  # Naukri limit on the comma-joined key skills string


@dataclass
class DailyPlan:
    headline_index: int
    headline: str
    skills_offset: int
    skills_add: list[str]
    skills_remove: list[str]
    it_index: int
    it_skill: str

    def describe(self) -> str:
        return (
            f"Headline  : variant {self.headline_index}: {self.headline}\n"
            f"Key skills: add {self.skills_add}\n"
            f"            remove {self.skills_remove}\n"
            f"IT skills : re-save '{self.it_skill}'"
        )


def load_config(path: Path) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    errors = []
    variants = cfg["headline"]["variants"]
    if len(variants) < 2:
        errors.append("headline.variants needs at least 2 entries")
    for i, v in enumerate(variants):
        if len(v) > HEADLINE_MAX:
            errors.append(f"headline variant {i} is {len(v)} chars (max {HEADLINE_MAX})")

    ks = cfg["key_skills"]
    core, pool = ks["core"], ks["rotation_pool"]
    budget = pool_budget(core)
    if len(pool) < 2:
        errors.append("key_skills.rotation_pool needs at least 2 entries")
    for s in pool:
        if len(s) + 1 > budget:
            errors.append(f"pool skill {s!r} needs {len(s) + 1} chars but only {budget} are free after core skills")
    overlap = {s.lower() for s in core} & {s.lower() for s in pool}
    if overlap:
        errors.append(f"skills in both core and rotation_pool: {sorted(overlap)}")

    if not cfg["it_skills"]["touch"]:
        errors.append("it_skills.touch needs at least 1 entry")

    for name in [*variants, *core, *pool, *cfg["it_skills"]["touch"]]:
        if "'" in name:
            errors.append(f"apostrophes aren't supported: {name!r}")
    if errors:
        raise ValueError("Invalid config.yaml:\n  - " + "\n  - ".join(errors))


def pool_budget(core: list[str]) -> int:
    """Characters left for rotating skills (each costs len + 1 for its comma)."""
    return KEY_SKILLS_CHARS - len(",".join(core))


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    # Index 0 = what's on the profile today, so the first run moves to the next one.
    return {"headline_index": 0, "skills_offset": 0, "it_index": -1, "history": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["history"] = state.get("history", [])[-60:]
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def plan_today(cfg: dict, state: dict) -> DailyPlan:
    variants = cfg["headline"]["variants"]
    hi = (state["headline_index"] + 1) % len(variants)

    ks = cfg["key_skills"]
    pool = ks["rotation_pool"]
    off = (state["skills_offset"] + 1) % len(pool)
    budget, used, active = pool_budget(ks["core"]), 0, []
    for i in range(len(pool)):
        skill = pool[(off + i) % len(pool)]
        if used + len(skill) + 1 > budget:
            break
        active.append(skill)
        used += len(skill) + 1
    inactive = [s for s in pool if s not in active] + list(ks.get("remove_if_present") or [])

    it = cfg["it_skills"]["touch"]
    ii = (state["it_index"] + 1) % len(it)

    return DailyPlan(hi, variants[hi], off, active, inactive, ii, it[ii])


def record_run(state: dict, plan: DailyPlan, results: dict[str, tuple[str, str]]) -> dict:
    """Advance rotation only for sections that were saved ("ok" or partially, "warn")."""
    saved = ("ok", "warn")
    if results.get("headline", ("",))[0] in saved:
        state["headline_index"] = plan.headline_index
    if results.get("keyskills", ("",))[0] in saved:
        state["skills_offset"] = plan.skills_offset
    if results.get("itskills", ("",))[0] in saved:
        state["it_index"] = plan.it_index
    state.setdefault("history", []).append(
        {"at": datetime.now().isoformat(timespec="seconds"), "results": {k: list(v) for k, v in results.items()}}
    )
    return state
