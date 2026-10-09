"""One profile-update run for one user - shared by the CLI and the Telegram bot."""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from naukri import sections
from naukri.browser import NaukriBrowser
from naukri.rotation import plan_today, record_run, save_state

log = logging.getLogger(__name__)

STEPS = ("headline", "keyskills", "itskills")
Results = dict[str, tuple[str, dict]]


def snapshot(page, log_dir: Path, name: str) -> Path:
    """Screenshot + HTML of the current page, for debugging failures."""
    folder = log_dir / f"{datetime.now():%Y-%m-%d}"
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"{datetime.now():%H%M%S}-{name}"
    try:
        page.screenshot(path=f"{stem}.png", full_page=True)
        Path(f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:  # never let debugging mask the real error
        log.warning("Snapshot failed: %s", e)
    return Path(f"{stem}.png")


def load_toggle_state(path: Path) -> dict:
    state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    # Files written by the old rotation mode lack some of these keys.
    for key, default in (("skill_index", -1), ("it_index", -1), ("history", [])):
        state.setdefault(key, default)
    return state


def run_update(
    store,
    state_path: Path,
    log_dir: Path,
    rotate_cfg: Optional[dict] = None,
    dry_run: bool = False,
    headless: bool = True,
    only: Optional[set[str]] = None,
    email: Optional[str] = None,
    password: Optional[str] = None,
    otp_provider: Optional[Callable[[], str]] = None,
    on_failure_shot: Optional[Callable[[Path, str], None]] = None,
) -> tuple[Results, str]:
    """Run the three steps once. rotate_cfg = config.yaml dict (your rotation lists);
    None = toggle mode (works from the user's own live profile only).

    Raises naukri.browser.LoginError if the session expired and can't be renewed.
    """
    only = only or set(STEPS)
    toggle = rotate_cfg is None
    if toggle:
        state = load_toggle_state(state_path)
    else:
        from naukri.rotation import load_state

        state = load_state(state_path)
        plan = plan_today(rotate_cfg, state)
        log.info("Plan:\n%s", plan.describe())

    picked: dict[str, int] = {}

    def keyskills_toggle(page):
        chips = sections.current_key_skills(page)
        if not chips:
            raise sections.SectionError("No key skills on the profile")
        i = (state["skill_index"] + 1) % len(chips)
        picked["skill_index"] = i
        return sections.update_key_skills(page, [chips[i]], [chips[i]], dry_run, atomic=True)

    def itskills_toggle(page):
        rows = sections.list_it_skills(page)
        if not rows:
            raise sections.SectionError("No rows in the IT skills section")
        i = (state["it_index"] + 1) % len(rows)
        picked["it_index"] = i
        return sections.touch_it_skill(page, rows[i], dry_run)

    if toggle:
        steps = {
            # Re-save the headline exactly as it is.
            "headline": lambda p: sections.update_headline(p, lambda current: current, dry_run),
            "keyskills": keyskills_toggle,
            "itskills": itskills_toggle,
        }
    else:
        steps = {
            "headline": lambda p: sections.update_headline(p, plan.headline, dry_run),
            "keyskills": lambda p: sections.update_key_skills(p, plan.skills_add, plan.skills_remove, dry_run),
            "itskills": lambda p: sections.touch_it_skill(p, plan.it_skill, dry_run),
        }

    results: Results = {}
    with NaukriBrowser(store, headless=headless, slow_mo=0 if headless else 250) as nb:
        try:
            nb.ensure_logged_in(email, password, otp_provider)
        except Exception:
            shot = snapshot(nb.page, log_dir, "login")
            if on_failure_shot:
                on_failure_shot(shot, "login failure")
            raise

        for name in STEPS:
            if name not in only:
                continue
            for attempt in (1, 2):
                try:
                    if attempt > 1 or name != STEPS[0]:
                        nb.open_profile()
                    results[name] = ("ok", steps[name](nb.page))
                    log.info("%s: %s", name, results[name][1])
                    break
                except sections.PartialSuccess as e:
                    results[name] = ("warn", e.data)
                    log.warning("%s: %s %s", name, e, e.data)
                    break
                except Exception as e:
                    log.exception("%s failed (attempt %s)", name, attempt)
                    shot = snapshot(nb.page, log_dir, name)
                    if attempt == 2:
                        results[name] = ("fail", {"error": str(e).splitlines()[0][:200]})
                        if on_failure_shot:
                            on_failure_shot(shot, f"{name} failed")
                    else:
                        nb.page.wait_for_timeout(random.randint(3_000, 8_000))

        # Informational only - never fail a run because this last read didn't work.
        try:
            nb.open_profile()
            last_updated = sections.last_updated_text(nb.page)
        except Exception as e:
            log.warning("Could not read 'last updated': %s", e)
            last_updated = "Profile last updated - unknown"
        nb.save_session()

    if not dry_run:
        if toggle:
            saved = ("ok", "warn")
            if results.get("keyskills", ("",))[0] in saved and "skill_index" in picked:
                state["skill_index"] = picked["skill_index"]
            if results.get("itskills", ("",))[0] in saved and "it_index" in picked:
                state["it_index"] = picked["it_index"]
            state.setdefault("history", []).append(
                {"at": datetime.now().isoformat(timespec="seconds"), "results": {k: list(v) for k, v in results.items()}}
            )
            save_state(state_path, state)
        else:
            save_state(state_path, record_run(state, plan, results))
    return results, last_updated
