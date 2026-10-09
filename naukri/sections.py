"""Updaters for the three profile sections.

Naukri's markup isn't documented, so every locator here is text/structure based
with fallbacks. If Naukri changes its UI, run `python main.py inspect` and adjust.
"""
from __future__ import annotations

import logging
import re

from typing import Callable

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PWTimeout

log = logging.getLogger(__name__)

SAVE = re.compile(r"^\s*save\s*$", re.I)
EDIT_CLASS = "contains(@class,'edit')"
CLOSE_CLASS = (
    "contains(@class,'close') or contains(@class,'cross') or "
    "contains(@class,'remove') or contains(@class,'delete')"
)
EDITOR_CONTAINERS = (
    "form:visible, [role='dialog']:visible, [class*='drawer' i]:visible, [class*='lightbox' i]:visible"
)


class SectionError(RuntimeError):
    pass


def _ci_text_eq(text: str) -> str:
    """XPath predicate: element's own text equals `text`, case-insensitive."""
    if "'" in text:
        raise ValueError(f"Apostrophes are not supported in names: {text!r}")
    upper, lower = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
    return f"translate(normalize-space(text()),'{upper}','{lower}')='{text.lower()}'"


def _title_xpath(title: str) -> str:
    return f"//*[{_ci_text_eq(title)}]"


def open_editor(page: Page, title: str) -> Locator:
    """Click the pencil icon next to a widget title and return the editor container."""
    candidates = [
        f"xpath=//span[contains(@class,'widgetTitle') and {_ci_text_eq(title)}]/following-sibling::*[{EDIT_CLASS}][1]",
        f"xpath={_title_xpath(title)}/following-sibling::*[{EDIT_CLASS}][1]",
        f"xpath={_title_xpath(title)}/parent::*/*[{EDIT_CLASS}][1]",
        f"xpath={_title_xpath(title)}/ancestor::*[position()<=3]//*[{EDIT_CLASS}][1]",
    ]
    for selector in candidates:
        icons = page.locator(selector)
        for i in range(icons.count()):
            icon = icons.nth(i)
            if icon.is_visible():
                icon.scroll_into_view_if_needed()
                icon.click()
                return _wait_for_editor(page, title)
    raise SectionError(f"Could not find the edit icon for '{title}'")


def _wait_for_editor(page: Page, title: str) -> Locator:
    editor = page.locator(EDITOR_CONTAINERS).filter(has=page.get_by_role("button", name=SAVE)).last
    try:
        editor.wait_for(state="visible", timeout=10_000)
    except PWTimeout:
        raise SectionError(f"Editor for '{title}' did not open")
    page.wait_for_timeout(800)
    return editor


def _save(page: Page, editor: Locator, title: str) -> None:
    editor.get_by_role("button", name=SAVE).first.click()
    try:
        editor.wait_for(state="hidden", timeout=15_000)
    except PWTimeout:
        errors = editor.locator("[class*='error' i]:visible").all_inner_texts()
        msg = "; ".join(e.strip() for e in errors if e.strip()) or "editor stayed open"
        raise SectionError(f"Saving '{title}' failed: {msg}")
    page.wait_for_timeout(1500)


def _cancel(page: Page) -> None:
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)


# --- Resume headline -------------------------------------------------------

def update_headline(page: Page, text: "str | Callable[[str], str]", dry_run: bool = False) -> dict:
    """Set the headline to `text`, or to text(current_headline) when given a function."""
    editor = open_editor(page, "Resume headline")
    box = editor.locator("textarea").first
    current = box.input_value().strip()
    if callable(text):
        text = text(current)
    if current == text.strip():
        log.info("Headline already matches today's variant - re-saving it")
    result = {"old": current, "new": text}
    if dry_run:
        _cancel(page)
        return result
    box.fill(text)
    _save(page, editor, "Resume headline")

    page.reload(wait_until="domcontentloaded")
    snippet = text[:40]
    try:
        page.get_by_text(snippet).first.wait_for(timeout=15_000)
    except PWTimeout:
        raise SectionError("Headline saved but the new text is not showing on the profile")
    return result


# --- Key skills ------------------------------------------------------------

def _chip_close(editor: Locator, skill: str) -> Locator:
    """Close/cross icon of a selected skill chip (empty locator if the chip isn't there)."""
    return editor.locator(
        f"xpath=.//*[{_ci_text_eq(skill)}]"
        f"/ancestor-or-self::*[position()<=3][.//*[{CLOSE_CLASS}]][1]"
        f"//*[{CLOSE_CLASS}]"
    )


class PartialSuccess(Exception):
    """Section was saved (or left as is), but not every planned change could be made."""

    def __init__(self, message: str, data: dict):
        super().__init__(message)
        self.data = data


def _chips(editor: Locator) -> list[str]:
    """Current key skills, from the hidden comma-joined value Naukri submits."""
    hidden = editor.locator("#chipsVal")
    if hidden.count():
        value = hidden.input_value()
        return [s for s in value.split(",") if s] if value else []
    return [t.strip() for t in editor.locator(".chipsContainer .chip .tagTxt").all_inner_texts()]


def current_key_skills(page: Page) -> list[str]:
    editor = open_editor(page, "Key skills")
    chips = _chips(editor)
    _cancel(page)
    return chips


def update_key_skills(
    page: Page, add: list[str], remove: list[str], dry_run: bool = False, atomic: bool = False
) -> dict:
    """Remove/add chips. Naukri caps the comma-joined list at KEY_SKILLS_CHARS.

    atomic=True: if any planned addition fails, close the editor without saving
    (used when re-adding a removed skill, so a skill can never be lost).
    """
    from naukri.rotation import KEY_SKILLS_CHARS

    editor = open_editor(page, "Key skills")
    removed, added, missing = [], [], []

    for skill in remove:
        close = _chip_close(editor, skill)
        if close.count():
            if not dry_run:
                close.first.click()
                page.wait_for_timeout(400)
            removed.append(skill)

    current = _chips(editor)
    if dry_run:
        current = [c for c in current if c.lower() not in {r.lower() for r in removed}]
    used = len(",".join(current))
    box = editor.locator("#keySkillSugg, input[name='suggestor']").first

    for skill in add:
        if any(c.lower() == skill.lower() for c in current):
            continue
        cost = len(skill) + (1 if current else 0)
        if used + cost > KEY_SKILLS_CHARS:
            missing.append(f"{skill} (no room: {used}/{KEY_SKILLS_CHARS} chars)")
            continue
        if dry_run:
            added.append(skill)
            current.append(skill)
            used += cost
            continue

        before = set(_chips(editor))
        box.click()
        box.fill("")
        box.press_sequentially(skill, delay=60)
        if box.input_value() != skill:  # typing was cut short (length cap) - never submit a fragment
            box.fill("")
            missing.append(f"{skill} (input rejected)")
            continue
        suggestion = editor.locator("#sugLst_keySkillSugg li:visible, .sugCont li:visible").filter(
            has_text=re.compile(rf"^\s*{re.escape(skill)}\s*$", re.I)
        )
        try:
            suggestion.first.click(timeout=8_000)
        except PWTimeout:
            box.press("Enter")
        page.wait_for_timeout(600)

        new = [c for c in _chips(editor) if c not in before]
        for chip in new:
            if chip.lower() != skill.lower():  # Naukri picked something else - undo it
                log.warning("Removing unexpected chip %r added while typing %r", chip, skill)
                _chip_close(editor, chip).first.click()
                page.wait_for_timeout(400)
        if any(c.lower() == skill.lower() for c in new):
            added.append(skill)
            current.append(skill)
            used = len(",".join(_chips(editor)))
        else:
            box.fill("")
            missing.append(f"{skill} (not accepted)")

    if not dry_run:
        current = _chips(editor)
    data = {
        "removed": removed,
        "added": added,
        "missing": missing,
        "total": len(current),
        "chars": len(",".join(current)),
        "limit": KEY_SKILLS_CHARS,
    }
    if missing:
        log.warning("Key skills not added: %s", "; ".join(missing))
    if dry_run:
        _cancel(page)
        return data
    if not removed and not added:
        _cancel(page)
        raise PartialSuccess("nothing to change", data)
    if missing and atomic:
        _cancel(page)
        data["removed"], data["added"] = [], []
        raise PartialSuccess("not saved - a skill could not be re-added", data)
    _save(page, editor, "Key skills")
    if missing:
        raise PartialSuccess("some skills could not be added", data)
    return data


def check_skill_names(page: Page, names: list[str]) -> dict[str, list[str]]:
    """Look up each name in Naukri's key skills catalog. Never saves.

    Returns {name: [suggestions]}; a name is usable only if it appears in its own list.
    """
    editor = open_editor(page, "Key skills")
    # The input only accepts as many chars as are left in the 250 budget; free room
    # by removing the longest chip (the editor is cancelled, so nothing is saved).
    chips = _chips(editor)
    if chips:
        _chip_close(editor, max(chips, key=len)).first.click()
    box = editor.locator("#keySkillSugg")
    found = {}
    for name in names:
        box.fill("")
        box.press_sequentially(name, delay=50)
        items: list[str] = []
        for _ in range(16):
            page.wait_for_timeout(500)
            items = [t.strip() for t in editor.locator("#sugLst_keySkillSugg li").all_inner_texts()]
            if items:
                break
        found[name] = items
    box.fill("")
    _cancel(page)
    return found


# --- IT skills -------------------------------------------------------------

def list_it_skills(page: Page) -> list[str]:
    """Names of the rows in the IT skills section."""
    card = page.locator("#lazyITSkills")
    card.scroll_into_view_if_needed()
    rows = card.locator("li.collection[data-prefillid]")
    try:
        rows.first.wait_for(timeout=15_000)
    except PWTimeout:
        return []
    return [rows.nth(i).locator("span.col").first.inner_text().strip() for i in range(rows.count())]


def touch_it_skill(page: Page, skill: str, dry_run: bool = False) -> dict:
    """Open an existing IT skills row and save it again."""
    # The widget is lazy-loaded: #lazyITSkills stays empty until scrolled into view.
    card = page.locator("#lazyITSkills")
    if card.count():
        card.scroll_into_view_if_needed()
        try:
            card.locator(f"xpath=.//*[{_ci_text_eq(skill)}]").first.wait_for(timeout=15_000)
        except PWTimeout:
            raise SectionError(f"IT skill row '{skill}' not found (IT skills section loaded?)")
    else:
        # Fallback: nearest container of the "IT skills" heading that holds this skill
        # but not the Key skills widget.
        card = page.locator(
            f"xpath={_title_xpath('IT skills')}"
            f"/ancestor::*[.//*[{_ci_text_eq(skill)}] and not(.//*[{_ci_text_eq('Key skills')}])][1]"
        ).first
    if not card.count():
        raise SectionError(f"IT skill row '{skill}' not found")
    # Each row's pencil is labelled "Edit IT skill: <name>".
    edit = card.get_by_role("button", name=re.compile(rf"^Edit IT skill:\s*{re.escape(skill)}\s*$", re.I)).first
    if not edit.count():
        edit = card.locator(
            f"xpath=.//*[{_ci_text_eq(skill)}]/ancestor-or-self::*[position()<=4][.//*[{EDIT_CLASS}]][1]//*[{EDIT_CLASS}]"
        ).first
    if not edit.count():
        raise SectionError(f"Edit icon for IT skill '{skill}' not found")
    edit.scroll_into_view_if_needed()
    edit.click()
    editor = _wait_for_editor(page, f"IT skills / {skill}")

    values = editor.locator("input").evaluate_all("els => els.map(e => (e.value || '').toLowerCase())")
    if not any(skill.lower() in v for v in values):
        _cancel(page)
        raise SectionError(f"Opened editor does not belong to IT skill '{skill}'")
    if dry_run:
        _cancel(page)
        return {"skill": skill}
    _save(page, editor, f"IT skills / {skill}")
    return {"skill": skill}


# --- Misc ------------------------------------------------------------------

def last_updated_text(page: Page) -> str:
    """Return Naukri's 'Profile last updated - ...' line if it's visible."""
    loc = page.get_by_text(re.compile(r"last updated", re.I)).first
    try:
        loc.wait_for(timeout=5_000)
        return " ".join(loc.locator("xpath=..").inner_text().split())
    except PWTimeout:
        return "unknown"
