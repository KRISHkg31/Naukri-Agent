"""Telegram report (HTML) describing exactly what a run changed."""
from __future__ import annotations

from datetime import datetime
from html import escape


def _list(items: list[str]) -> str:
    return escape(", ".join(items)) if items else "—"


def format_report(results: dict[str, tuple[str, dict]], dry_run: bool, last_updated: str) -> str:
    ok = all(status == "ok" for status, _ in results.values())
    if dry_run:
        title = "🧪 <b>Naukri profile - DRY RUN</b>\n<i>Nothing was saved. This is what a real run would do.</i>"
    elif ok:
        title = "✅ <b>Naukri profile updated</b>"
    else:
        title = "⚠️ <b>Naukri profile update - needs attention</b>"
    lines = [title, f"🗓 {datetime.now():%a, %d %b %Y · %I:%M %p}"]
    verb = (lambda done, would: would) if dry_run else (lambda done, would: done)

    for name, (status, data) in results.items():
        lines.append("")
        if status == "fail":
            label = {"headline": "Resume headline", "keyskills": "Key skills", "itskills": "IT skills"}[name]
            lines.append(f"❌ <b>{label}</b> - failed")
            lines.append(f"<i>{escape(data.get('error', 'unknown error'))}</i>")
            continue

        if name == "headline":
            lines.append("📝 <b>Resume headline</b>")
            if data["old"] == data["new"]:
                lines.append(f"{verb('Re-saved', 'Would re-save')} (unchanged):\n{escape(data['new'])}")
            else:
                lines.append(f"<b>Before:</b> {escape(data['old'])}")
                lines.append(f"<b>{verb('After', 'Will be')}:</b> {escape(data['new'])}")

        elif name == "keyskills":
            lines.append("🛠 <b>Key skills</b>")
            if data["added"] and {s.lower() for s in data["added"]} == {s.lower() for s in data["removed"]}:
                lines.append(f"🔁 {verb('Refreshed', 'Will refresh')} (removed and re-added): {_list(data['added'])}")
            else:
                lines.append(f"➕ {verb('Added', 'Will add')}: {_list(data['added'])}")
                lines.append(f"➖ {verb('Removed', 'Will remove')}: {_list(data['removed'])}")
            if data["missing"]:
                lines.append(f"⚠️ Could not add: {_list(data['missing'])}")
            lines.append(f"📊 {data['total']} skills · {data['chars']}/{data['limit']} characters")
            if status == "warn" and not data["added"] and not data["removed"]:
                lines.append("<i>Nothing changed in this section.</i>")

        elif name == "itskills":
            lines.append("💻 <b>IT skills</b>")
            lines.append(f"🔄 {verb('Re-saved', 'Will re-save')}: {escape(data['skill'])}")

    lines += ["", f"🕒 {escape(last_updated)}"]
    return "\n".join(lines)
