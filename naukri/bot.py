"""Multi-user Telegram bot: onboarding, commands, and the 9/10/11/12 schedule.

One process, three threads:
  - main thread: long-polls Telegram and handles messages (never touches the browser)
  - worker: runs browser jobs strictly one at a time (logins, updates)
  - scheduler: queues "update everyone" at each slot in schedule.run_hours
"""
from __future__ import annotations

import json
import logging
import queue
import random
import re
import threading
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Callable, Optional

import requests

from naukri.browser import LoginError, NaukriBrowser
from naukri.notifier import Notifier
from naukri.jobsearch import QUICK_PICKS, find_jobs, format_jobs, parse_query
from naukri.report import format_report
from naukri.runner import run_update
from naukri.users import User, all_users

log = logging.getLogger(__name__)

HELP = """<b>Naukri profile bot</b>
I re-save your Naukri profile at {hours} IST every day so it shows as recently updated - nothing is added or removed.

/findjob devops - jobs posted in the last 24 h (add your years of experience: /findjob backend 2)
/login - connect your Naukri account
/status - your status and last run
/dryrun - test run, saves nothing
/runnow - update your profile now
/pause - stop daily updates
/resume - restart daily updates
/delete - remove your account and all saved data
/cancel - cancel the current step"""

ADMIN_HELP = """

<b>Admin</b>
/users - list users
/approve - approve the pending user (or tap ✅ Approve on the request)
/deny - reject the pending user (or tap ❌ Deny)
/remove &lt;id&gt; - delete a user and their data"""

MENU = [
    ("findjob", "Search jobs posted in the last 24 hours"),
    ("login", "Connect your Naukri account"),
    ("status", "Your status and last run"),
    ("dryrun", "Test run, saves nothing"),
    ("runnow", "Update your profile now"),
    ("pause", "Stop daily updates"),
    ("resume", "Restart daily updates"),
    ("delete", "Remove your account and data"),
    ("cancel", "Cancel the current step"),
    ("help", "Help"),
]


class Bot:
    def __init__(self, token: str, admin_id: str, users_root: Path, session_key: str,
                 run_hours: list[int], jitter_s: int, state_file: Path):
        self.token = token
        self.admin_id = str(admin_id)
        self.root = users_root
        self.key = session_key
        self.hours = sorted(run_hours)
        self.jitter_s = jitter_s
        self.state_file = state_file
        self.jobs: "queue.Queue[tuple[str, Callable[[], None]]]" = queue.Queue()
        self.conv: dict[str, dict] = {}          # chat -> {"step": "email"|"password", "email": ...}
        self.otp_waiters: dict[str, queue.Queue] = {}
        self.busy: Optional[str] = None
        self.root.mkdir(parents=True, exist_ok=True)

    # --- Telegram plumbing ------------------------------------------------

    def api(self, method: str, http_timeout: int = 20, **data) -> dict:
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", data=data, timeout=http_timeout)
        if not r.ok:
            log.warning("Telegram %s failed: %s", method, r.text[:200])
            return {}
        return r.json()

    def send(self, chat: str, text: str, buttons: Optional[list[list[tuple[str, str]]]] = None) -> None:
        """buttons: rows of (label, callback_data) shown under the message."""
        extra = {}
        if buttons:
            extra["reply_markup"] = json.dumps(
                {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
            )
        for i in range(0, len(text), 3900):
            self.api("sendMessage", chat_id=chat, text=text[i : i + 3900], parse_mode="HTML",
                     disable_web_page_preview="true", **extra)

    def run(self) -> None:
        self.api("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in MENU]))
        threading.Thread(target=self._worker, daemon=True, name="worker").start()
        threading.Thread(target=self._scheduler, daemon=True, name="scheduler").start()
        log.info("Bot started; schedule %s IST; %d users", self.hours, len(all_users(self.root)))
        offset = None
        while True:
            try:
                data = {"timeout": 25, **({"offset": offset} if offset else {})}
                updates = self.api("getUpdates", http_timeout=35, **data).get("result", [])
            except requests.RequestException as e:
                log.warning("Polling failed: %s", e)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    try:
                        self.on_button(u["callback_query"])
                    except Exception:
                        log.exception("Handling button failed")
                    continue
                msg = u.get("message") or {}
                if msg.get("chat", {}).get("type") == "private" and "text" in msg:
                    try:
                        self.handle(msg)
                    except Exception:
                        log.exception("Handling message failed")

    # --- Message handling ---------------------------------------------------

    def handle(self, msg: dict) -> None:
        chat = str(msg["chat"]["id"])
        text = msg["text"].strip()
        name = " ".join(filter(None, [msg["from"].get("first_name"), msg["from"].get("last_name")])) or chat

        if chat in self.otp_waiters and not text.startswith("/"):
            self.otp_waiters[chat].put(text)
            return

        conv = self.conv.get(chat)
        if conv and not text.startswith("/"):
            return self._continue_login(chat, msg, text, conv)

        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        user = User(self.root, chat)
        is_admin = chat == self.admin_id

        if cmd in ("/start", "/help"):
            if not user.exists:
                if is_admin:
                    user.create(name, "approved")
                else:
                    user.create(name, "pending")
                    self.send(chat, "👋 Hi! Your request has been sent to the admin. I'll message you once you're approved.")
                    self.send(self.admin_id, f"🙋 <b>{escape(name)}</b> wants to use the bot.",
                              buttons=self._decision_buttons(chat))
                    return
            if user.status == "pending":
                return self.send(chat, "⏳ Still waiting for the admin to approve you.")
            self.send(chat, HELP.format(hours=self._hours_text()) + (ADMIN_HELP if is_admin else ""))
            if user.status == "approved":
                self.send(chat, "Next step: send /login to connect your Naukri account.")
            return

        if is_admin and cmd in ("/users", "/approve", "/deny", "/remove"):
            return self._admin(cmd, arg.strip())

        if not user.exists or user.status == "pending":
            return self.send(chat, "Send /start first." if not user.exists else "⏳ Waiting for admin approval.")

        if cmd == "/cancel":
            self.conv.pop(chat, None)
            return self.send(chat, "Cancelled.")
        if cmd == "/findjob":
            return self._findjob_request(chat, arg)
        if cmd == "/login":
            self.conv[chat] = {"step": "email"}
            return self.send(chat, "🔐 <b>Connect Naukri</b>\n\nSend the <b>email</b> you use to log in to Naukri.\n"
                                   "(Your password is used once to log in, then deleted - only the login session is kept, encrypted.)")
        if cmd == "/status":
            return self.send(chat, self._status_text(user))
        if cmd in ("/dryrun", "/runnow"):
            if user.status not in ("active", "paused"):
                return self.send(chat, "Connect your account first with /login.")
            dry = cmd == "/dryrun"
            self._enqueue(chat, f"{'dry run' if dry else 'update'} for {user.name}", lambda: self._run_user(user, dry))
            return
        if cmd == "/pause":
            if user.status == "active":
                user.save(status="paused")
            return self.send(chat, "⏸ Daily updates paused. Send /resume to restart.")
        if cmd == "/resume":
            if user.status == "paused":
                user.save(status="active")
                return self.send(chat, f"▶️ Resumed. Next update at {self._next_slot():%I:%M %p}.")
            return self.send(chat, "You're not paused." if user.status == "active" else "Connect first with /login.")
        if cmd == "/delete":
            if arg.strip().lower() != "yes":
                return self.send(chat, "⚠️ This deletes your saved Naukri session and all bot data.\n"
                                       "Send <code>/delete yes</code> to confirm.")
            user.delete()
            self.conv.pop(chat, None)
            self.send(chat, "🗑 Done - all your data was deleted. Send /start if you want to come back.")
            if not is_admin:
                self.send(self.admin_id, f"ℹ️ {escape(user.name)} ({chat}) deleted their account.")
            return
        self.send(chat, "I didn't understand that. Send /help.")

    def _continue_login(self, chat: str, msg: dict, text: str, conv: dict) -> None:
        if conv["step"] == "email":
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                return self.send(chat, "That doesn't look like an email - try again, or /cancel.")
            conv.update(step="password", email=text)
            self.send(chat, "Now send your Naukri <b>password</b>. I'll delete your message right after reading it.")
            return
        # password step
        self.api("deleteMessage", chat_id=chat, message_id=msg["message_id"])
        email, password = conv["email"], text
        self.conv.pop(chat, None)
        user = User(self.root, chat)
        self._enqueue(chat, f"login for {user.name}", lambda: self._login(user, email, password))

    def _admin(self, cmd: str, arg: str) -> None:
        if cmd == "/users":
            users = all_users(self.root)
            lines = [f"<b>{len(users)} user(s)</b>"] + [
                f"• {escape(u.name)} <code>{u.id}</code> - {u.status} - last: {u.meta.get('last_run', 'never')}"
                for u in users
            ]
            return self.send(self.admin_id, "\n".join(lines))
        if not arg and cmd in ("/approve", "/deny"):
            pending = [u for u in all_users(self.root) if u.status == "pending"]
            if not pending:
                return self.send(self.admin_id, "No pending requests.")
            if len(pending) > 1:
                for u in pending:
                    self.send(self.admin_id, f"🙋 <b>{escape(u.name)}</b> is waiting.", buttons=self._decision_buttons(u.id))
                return
            arg = pending[0].id
        target = User(self.root, arg)
        if not arg or not target.exists:
            return self.send(self.admin_id, f"No user <code>{escape(arg)}</code>. See /users.")
        if cmd == "/approve" and target.status != "pending":
            return self.send(self.admin_id, f"{escape(target.name)} is already approved ({target.status}).")
        if cmd == "/approve":
            target.save(status="approved")
            self.send(target.id, "✅ You're approved! Send /login to connect your Naukri account.")
            self.send(self.admin_id, f"Approved {escape(target.name)}.")
        elif cmd in ("/deny", "/remove"):
            target.delete()
            self.send(target.id, "Your access to this bot was removed." if cmd == "/remove"
                      else "Sorry, your request was not approved.")
            self.send(self.admin_id, f"Removed {escape(target.name)} and their data.")

    @staticmethod
    def _decision_buttons(uid: str) -> list[list[tuple[str, str]]]:
        return [[("✅ Approve", f"approve:{uid}"), ("❌ Deny", f"deny:{uid}")]]

    def on_button(self, cq: dict) -> None:
        """Inline buttons: /findjob quick picks (any approved user), Approve / Deny (admin only)."""
        self.api("answerCallbackQuery", callback_query_id=cq["id"])
        presser = str(cq.get("from", {}).get("id"))
        action, _, uid = (cq.get("data") or "").partition(":")
        if action == "findjob":
            user = User(self.root, presser)
            if user.exists and user.status != "pending":
                self._findjob_request(presser, uid)
            return
        if presser != self.admin_id:
            return
        if action not in ("approve", "deny") or not uid:
            return
        msg = cq.get("message") or {}
        if msg:  # remove the buttons so they can't be pressed twice
            self.api("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                     reply_markup=json.dumps({"inline_keyboard": []}))
        self._admin(f"/{action}", uid)

    def _findjob_request(self, chat: str, query: str) -> None:
        keyword, exp = parse_query(query)
        if not keyword:
            rows = [QUICK_PICKS[:3], QUICK_PICKS[3:]]
            return self.send(chat, "🔎 Which jobs should I look for? Tap one, or type e.g. "
                                   "<code>/findjob devops 2</code> (the number = your years of experience, optional).",
                             buttons=[[(label, f"findjob:{key}") for label, key in row] for row in rows])
        self._enqueue(chat, f"findjob {keyword!r} for {chat}", lambda: self._findjob(chat, keyword, exp))

    def _findjob(self, chat: str, keyword: str, exp: Optional[int]) -> None:
        try:
            jobs, total, matched = find_jobs(keyword, exp)
        except Exception as e:
            log.exception("findjob failed")
            return self.send(chat, f"❌ Job search failed: {escape(str(e).splitlines()[0][:150])}")
        self.send(chat, format_jobs(keyword, exp, jobs, total, matched))

    # --- Jobs (worker thread) -------------------------------------------------

    def _enqueue(self, chat: str, label: str, fn: Callable[[], None]) -> None:
        ahead = self.jobs.qsize() + (1 if self.busy else 0)
        self.jobs.put((label, fn))
        self.send(chat, "⏳ Working on it…" if not ahead else f"⏳ Queued ({ahead} job(s) ahead of you)…")

    def _worker(self) -> None:
        while True:
            label, fn = self.jobs.get()
            self.busy = label
            log.info("Job start: %s", label)
            try:
                fn()
            except Exception:
                log.exception("Job crashed: %s", label)
            finally:
                self.busy = None
                log.info("Job done: %s", label)

    def _ask_otp(self, chat: str) -> str:
        q: queue.Queue = queue.Queue()
        self.otp_waiters[chat] = q
        self.send(chat, "📲 Naukri sent you an <b>OTP</b> (SMS/email). Reply with the code within 10 minutes.")
        try:
            while True:
                reply = q.get(timeout=600)
                m = re.search(r"\b(\d{4,8})\b", reply)
                if m:
                    self.send(chat, "👍 Got it, entering the OTP…")
                    return m.group(1)
                self.send(chat, "Please send just the numeric code.")
        except queue.Empty:
            raise LoginError("No OTP received within 10 minutes")
        finally:
            self.otp_waiters.pop(chat, None)

    def _login(self, user: User, email: str, password: str) -> None:
        try:
            with NaukriBrowser(user.store(self.key), fresh=True) as nb:
                nb.login(email, password, otp_provider=lambda: self._ask_otp(user.id))
                headline = nb.page.locator("#lazyResumeHead, .resumeHeadline").first
                raw = headline.inner_text() if headline.count() else ""
                raw = raw.replace("editOneTheme", "").strip()
                raw = raw[len("Resume headline"):] if raw.lower().startswith("resume headline") else raw
                headline_text = " ".join(raw.split())[:200]
        except Exception as e:
            log.warning("Login failed for %s: %s", user.id, e)
            return self.send(user.id, f"❌ Login failed: {escape(str(e).splitlines()[0][:200])}\n\n"
                                      "Check your email/password and try /login again.")
        finally:
            del password
        user.save(status="active", email_hint=_mask(email), connected=datetime.now().isoformat(timespec="seconds"))
        self.send(user.id, "✅ <b>Naukri connected!</b>\n"
                           + (f"<i>{escape(headline_text)}</i>\n\n" if headline_text else "\n")
                           + f"Your profile will be refreshed daily at {self._hours_text()} IST.\n"
                           "Try /dryrun to see what a run does (nothing is saved).")
        if user.id != self.admin_id:
            self.send(self.admin_id, f"🔗 {escape(user.name)} connected their Naukri account.")

    def _run_user(self, user: User, dry_run: bool) -> None:
        user = User(self.root, user.id)  # reload: status may have changed while queued
        if not user.exists:
            return
        notifier = Notifier(self.token, user.id)
        try:
            results, last_updated = run_update(
                user.store(self.key), user.state_path, user.log_dir,
                rotate_cfg=None, dry_run=dry_run,  # re-save only: nothing added or removed
                on_failure_shot=lambda shot, caption: notifier.send_photo(shot, caption),
            )
        except LoginError:
            user.save(status="needs_login")
            return self.send(user.id, "🔒 Your Naukri session has expired. Send /login to reconnect "
                                      "(daily updates are on hold until then).")
        except Exception as e:
            log.exception("Run failed for %s", user.id)
            return self.send(user.id, f"❌ Update failed: {escape(str(e).splitlines()[0][:200])}")
        if not dry_run:
            ok = all(s == "ok" for s, _ in results.values())
            user.save(last_run=datetime.now().strftime("%d %b %I:%M %p"), last_result="ok" if ok else "warning")
        self.send(user.id, format_report(results, dry_run, last_updated))

    def _run_everyone(self, slot: str) -> None:
        users = [u for u in all_users(self.root) if u.status == "active"]
        log.info("Slot %s: updating %d user(s)", slot, len(users))
        random.shuffle(users)
        for u in users:
            time.sleep(random.randint(0, self.jitter_s))
            self._run_user(u, dry_run=False)

    # --- Scheduler --------------------------------------------------------------

    def _scheduler(self) -> None:
        done = set(json.loads(self.state_file.read_text())) if self.state_file.exists() else set()
        while True:
            now = datetime.now()
            for h in self.hours:
                slot = now.replace(hour=h, minute=0, second=0, microsecond=0)
                key = slot.strftime("%Y-%m-%d %H:00")
                # Run a missed slot only within its own hour (e.g. after a reboot at 09:20).
                if key not in done and slot <= now < slot + timedelta(minutes=55):
                    done.add(key)
                    done = {k for k in done if k >= (now - timedelta(days=2)).strftime("%Y-%m-%d")}
                    self.state_file.write_text(json.dumps(sorted(done)))
                    self.jobs.put((f"scheduled {key}", lambda k=key: self._run_everyone(k)))
            time.sleep(30)

    # --- Helpers ------------------------------------------------------------------

    def _hours_text(self) -> str:
        return ", ".join(datetime(2000, 1, 1, h).strftime("%I %p").lstrip("0") for h in self.hours)

    def _next_slot(self) -> datetime:
        now = datetime.now()
        for day in (0, 1):
            for h in self.hours:
                t = (now + timedelta(days=day)).replace(hour=h, minute=0, second=0, microsecond=0)
                if t > now:
                    return t
        return now

    def _status_text(self, user: User) -> str:
        labels = {"approved": "Not connected - send /login", "active": "✅ Active",
                  "needs_login": "🔒 Session expired - send /login", "paused": "⏸ Paused"}
        lines = [f"<b>Status:</b> {labels.get(user.status, user.status)}"]
        if user.meta.get("email_hint"):
            lines.append(f"<b>Naukri account:</b> {escape(user.meta['email_hint'])}")
        lines.append("<b>Mode:</b> re-save only (nothing is added or removed)")
        lines.append(f"<b>Last update:</b> {user.meta.get('last_run', 'never')}"
                     + (" (⚠️ partial)" if user.meta.get("last_result") == "warning" else ""))
        if user.status == "active":
            lines.append(f"<b>Next update:</b> {self._next_slot():%a %I:%M %p}")
        return "\n".join(lines)


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}{'*' * max(1, len(name) - 2)}@{domain}"
