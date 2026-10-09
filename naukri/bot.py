"""Multi-user bot over Telegram and/or WhatsApp: onboarding, commands, and the daily schedule.

Threads:
  - Telegram long polling (main thread) and/or the WhatsApp webhook server
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

from naukri.browser import LoginError, NaukriBrowser
from naukri.channels import LoginPage, TelegramChannel, WhatsAppChannel
from naukri.report import format_report, summary_line
from naukri.runner import run_update
from naukri.users import User, all_users

log = logging.getLogger(__name__)

COMMANDS = ("start", "help", "login", "status", "report", "dryrun", "runnow", "pause", "resume",
            "delete", "cancel", "users", "approve", "deny", "remove")
ADMIN_COMMANDS = ("users", "approve", "deny", "remove")
MENU = [
    ("login", "Connect your Naukri account"),
    ("status", "Your status and last run"),
    ("report", "Show the last update report"),
    ("dryrun", "Test run, saves nothing"),
    ("runnow", "Update your profile now"),
    ("pause", "Stop daily updates"),
    ("resume", "Restart daily updates"),
    ("delete", "Remove your account and data"),
    ("cancel", "Cancel the current step"),
    ("help", "Help"),
]
WA_WINDOW = timedelta(hours=23)  # WhatsApp allows free-form replies for 24 h after the user's last message


class Bot:
    def __init__(self, admin_id: str, users_root: Path, session_key: str, run_hours: list[int], jitter_s: int,
                 state_file: Path, telegram: Optional[TelegramChannel], whatsapp: Optional[WhatsAppChannel]):
        self.admin_id = str(admin_id)
        self.root = users_root
        self.key = session_key
        self.hours = sorted(run_hours)
        self.jitter_s = jitter_s
        self.state_file = state_file
        self.tg, self.wa = telegram, whatsapp
        self.login_page = LoginPage(self._login_submitted) if whatsapp else None
        self.jobs: "queue.Queue[tuple[str, Callable[[], None]]]" = queue.Queue()
        self.conv: dict[str, dict] = {}          # Telegram in-chat login: uid -> {"step", "email"}
        self.otp_waiters: dict[str, queue.Queue] = {}
        self.busy: Optional[str] = None
        self.lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)

    # --- Sending ---------------------------------------------------------------

    def _channel(self, uid: str):
        ch = self.wa if uid.startswith("wa-") else self.tg
        if ch is None:
            raise RuntimeError(f"No channel configured for user {uid}")
        return ch

    def _window_open(self, uid: str) -> bool:
        last = User(self.root, uid).meta.get("last_inbound")
        return bool(last) and datetime.now() - datetime.fromisoformat(last) < WA_WINDOW

    def send(self, uid: str, html: str, summary: Optional[str] = None) -> None:
        """Send to a user on their channel. WhatsApp users outside the 24 h window get the
        approved template with a one-line `summary` instead of the full message."""
        try:
            ch = self._channel(uid)
            if ch is self.wa and not self._window_open(uid):
                ch.send_template(uid, (summary or "you have a new message") + ". Reply REPORT for details")
                user = User(self.root, uid)
                if user.exists:
                    user.save(unread=html)
                return
            ch.send(uid, html)
        except Exception:
            log.exception("Sending to %s failed", uid)

    def send_photo(self, uid: str, path: Path, caption: str) -> None:
        try:
            ch = self._channel(uid)
            if ch is self.wa and not self._window_open(uid):
                return
            ch.send_photo(uid, path, caption)
        except Exception:
            log.exception("Sending photo to %s failed", uid)

    # --- Startup -----------------------------------------------------------------

    def run(self) -> None:
        threading.Thread(target=self._worker, daemon=True, name="worker").start()
        threading.Thread(target=self._scheduler, daemon=True, name="scheduler").start()
        log.info("Bot started; channels=%s; schedule %s IST; %d users",
                 [c.name for c in (self.tg, self.wa) if c], self.hours, len(all_users(self.root)))
        if self.wa:
            server = threading.Thread(target=self.wa.serve_forever, args=(self.on_message, self.login_page),
                                      daemon=not self.tg, name="whatsapp")
            server.start()
        if self.tg:
            self.tg.set_commands(MENU)
            self.tg.poll_forever(self.on_message)
        else:
            server.join()

    # --- Incoming messages ----------------------------------------------------------

    def on_message(self, uid: str, name: str, text: str, ref: Optional[str]) -> None:
        with self.lock:
            self._handle(uid, name, text.strip(), ref)

    def _handle(self, uid: str, name: str, text: str, ref: Optional[str]) -> None:
        user = User(self.root, uid)
        is_wa = uid.startswith("wa-")
        if is_wa and user.exists:
            user.save(last_inbound=datetime.now().isoformat(timespec="seconds"))

        if uid in self.otp_waiters and not text.startswith("/"):
            self.otp_waiters[uid].put(text)
            return
        conv = self.conv.get(uid)
        if conv and not text.startswith("/"):
            return self._continue_chat_login(uid, ref, text, conv)

        word, _, arg = text.partition(" ")
        cmd = word.lower().lstrip("/").split("@")[0]
        if cmd not in COMMANDS:
            cmd = "start" if not user.exists else ""
        is_admin = uid == self.admin_id
        p = "" if is_wa else "/"

        if cmd in ("start", "help"):
            if not user.exists:
                user.create(name, "approved" if is_admin else "pending")
                if is_wa:
                    user.save(last_inbound=datetime.now().isoformat(timespec="seconds"))
                if not is_admin:
                    self.send(uid, "👋 Hi! Your request has been sent to the admin. I'll message you once you're approved.")
                    via = "WhatsApp" if is_wa else "Telegram"
                    self.send(self.admin_id, f"🙋 <b>{escape(name)}</b> wants to use the bot ({via}).\n"
                                             f"/approve {uid}   or   /deny {uid}")
                    return
            if user.status == "pending":
                return self.send(uid, "⏳ Still waiting for the admin to approve you.")
            self.send(uid, self._help(p, is_admin))
            if user.status == "approved":
                self.send(uid, f"Next step: send {p}login to connect your Naukri account.")
            return

        if cmd in ADMIN_COMMANDS:
            return self._admin(cmd, arg.strip()) if is_admin else self.send(uid, "That command is admin-only.")
        if user.status == "pending":
            return self.send(uid, "⏳ Waiting for admin approval.")

        if cmd == "cancel":
            self.conv.pop(uid, None)
            return self.send(uid, "Cancelled.")
        if cmd == "login":
            if is_wa:
                link = self.wa.login_url(self.login_page.new_token(uid))
                return self.send(uid, "🔐 <b>Connect Naukri</b>\n\nOpen this one-time link and enter your Naukri "
                                      f"login (valid 15 minutes):\n{link}\n\nYour password is never sent in this chat "
                                      "and never stored.")
            self.conv[uid] = {"step": "email"}
            return self.send(uid, "🔐 <b>Connect Naukri</b>\n\nSend the <b>email</b> you use to log in to Naukri.\n"
                                  "(Your password is used once to log in, then deleted - only the login session is kept, encrypted.)")
        if cmd == "status":
            return self.send(uid, self._status_text(user, p))
        if cmd == "report":
            report = user.meta.get("unread") or user.meta.get("last_report")
            if user.meta.get("unread"):
                user.save(unread=None)
            return self.send(uid, report or "No report yet.")
        if cmd in ("dryrun", "runnow"):
            if user.status not in ("active", "paused"):
                return self.send(uid, f"Connect your account first with {p}login.")
            dry = cmd == "dryrun"
            return self._enqueue(uid, f"{'dry run' if dry else 'update'} for {user.name}", lambda: self._run_user(uid, dry))
        if cmd == "pause":
            if user.status == "active":
                user.save(status="paused")
            return self.send(uid, f"⏸ Daily updates paused. Send {p}resume to restart.")
        if cmd == "resume":
            if user.status == "paused":
                user.save(status="active")
                return self.send(uid, f"▶️ Resumed. Next update at {self._next_slot():%I:%M %p}.")
            return self.send(uid, "You're not paused." if user.status == "active" else f"Connect first with {p}login.")
        if cmd == "delete":
            if arg.strip().lower() != "yes":
                return self.send(uid, "⚠️ This deletes your saved Naukri session and all bot data.\n"
                                      f"Send <code>{p}delete yes</code> to confirm.")
            self.send(uid, f"🗑 Done - all your data was deleted. Send {p}start if you want to come back.")
            user.delete()
            self.conv.pop(uid, None)
            if not is_admin:
                self.send(self.admin_id, f"ℹ️ {escape(user.name)} ({uid}) deleted their account.")
            return
        self.send(uid, f"I didn't understand that. Send {p}help.")

    def _continue_chat_login(self, uid: str, ref: Optional[str], text: str, conv: dict) -> None:
        if conv["step"] == "email":
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
                return self.send(uid, "That doesn't look like an email - try again, or /cancel.")
            conv.update(step="password", email=text)
            return self.send(uid, "Now send your Naukri <b>password</b>. I'll delete your message right after reading it.")
        self._channel(uid).delete(uid, ref)
        email = conv["email"]
        self.conv.pop(uid, None)
        self._login_submitted(uid, email, text)

    def _login_submitted(self, uid: str, email: str, password: str) -> None:
        name = User(self.root, uid).name
        self._enqueue(uid, f"login for {name}", lambda: self._login(uid, email, password))

    def _admin(self, cmd: str, arg: str) -> None:
        if cmd == "users":
            users = all_users(self.root)
            lines = [f"<b>{len(users)} user(s)</b>"] + [
                f"• {escape(u.name)} <code>{u.id}</code> - {u.status} - last: {u.meta.get('last_run', 'never')}"
                for u in users
            ]
            return self.send(self.admin_id, "\n".join(lines))
        target = User(self.root, arg)
        if not arg or not target.exists:
            return self.send(self.admin_id, f"No user <code>{escape(arg)}</code>. See /users.")
        if cmd == "approve":
            target.save(status="approved")
            p = "" if arg.startswith("wa-") else "/"
            self.send(target.id, f"✅ You're approved! Send {p}login to connect your Naukri account.",
                      summary="you're approved - send LOGIN to connect your Naukri account")
            self.send(self.admin_id, f"Approved {escape(target.name)}.")
        elif cmd in ("deny", "remove"):
            self.send(target.id, "Your access to this bot was removed." if cmd == "remove"
                      else "Sorry, your request was not approved.")
            target.delete()
            self.send(self.admin_id, f"Removed {escape(target.name)} and their data.")

    # --- Jobs (worker thread) ----------------------------------------------------------

    def _enqueue(self, uid: str, label: str, fn: Callable[[], None]) -> None:
        ahead = self.jobs.qsize() + (1 if self.busy else 0)
        self.jobs.put((label, fn))
        self.send(uid, "⏳ Working on it…" if not ahead else f"⏳ Queued ({ahead} job(s) ahead of you)…")

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

    def _ask_otp(self, uid: str) -> str:
        q: queue.Queue = queue.Queue()
        self.otp_waiters[uid] = q
        self.send(uid, "📲 Naukri sent you an <b>OTP</b> (SMS/email). Reply with the code within 10 minutes.",
                  summary="Naukri sent you an OTP - reply with the code within 10 minutes")
        try:
            while True:
                m = re.search(r"\b(\d{4,8})\b", q.get(timeout=600))
                if m:
                    self.send(uid, "👍 Got it, entering the OTP…")
                    return m.group(1)
                self.send(uid, "Please send just the numeric code.")
        except queue.Empty:
            raise LoginError("No OTP received within 10 minutes")
        finally:
            self.otp_waiters.pop(uid, None)

    def _login(self, uid: str, email: str, password: str) -> None:
        user = User(self.root, uid)
        p = "" if uid.startswith("wa-") else "/"
        try:
            with NaukriBrowser(user.store(self.key)) as nb:
                nb.login(email, password, otp_provider=lambda: self._ask_otp(uid))
                headline = nb.page.locator("#lazyResumeHead, .resumeHeadline").first
                headline_text = " ".join(headline.inner_text().split())[:200] if headline.count() else ""
        except Exception as e:
            log.warning("Login failed for %s: %s", uid, e)
            return self.send(uid, f"❌ Login failed: {escape(str(e).splitlines()[0][:200])}\n\n"
                                  f"Check your email/password and try {p}login again.",
                             summary="Naukri login failed - send LOGIN to try again")
        finally:
            del password
        user.save(status="active", email_hint=_mask(email), connected=datetime.now().isoformat(timespec="seconds"))
        self.send(uid, "✅ <b>Naukri connected!</b>\n"
                       + (f"<i>{escape(headline_text)}</i>\n\n" if headline_text else "\n")
                       + f"Your profile will be refreshed daily at {self._hours_text()} IST.\n"
                       f"Try {p}dryrun to see what a run does (nothing is saved).",
                  summary="Naukri connected - updates run daily")
        if uid != self.admin_id:
            self.send(self.admin_id, f"🔗 {escape(user.name)} connected their Naukri account.")

    def _run_user(self, uid: str, dry_run: bool) -> None:
        user = User(self.root, uid)  # fresh: status may have changed while queued
        if not user.exists:
            return
        p = "" if uid.startswith("wa-") else "/"
        try:
            results, last_updated = run_update(
                user.store(self.key), user.state_path, user.log_dir,
                rotate_cfg=user.rotate_cfg(), dry_run=dry_run,
                on_failure_shot=lambda shot, caption: self.send_photo(uid, shot, caption),
            )
        except LoginError:
            user.save(status="needs_login")
            return self.send(uid, f"🔒 Your Naukri session has expired. Send {p}login to reconnect "
                                  "(daily updates are on hold until then).",
                             summary="your Naukri session expired - send LOGIN to reconnect")
        except Exception as e:
            log.exception("Run failed for %s", uid)
            return self.send(uid, f"❌ Update failed: {escape(str(e).splitlines()[0][:200])}",
                             summary="profile update failed")
        report = format_report(results, dry_run, last_updated)
        if not dry_run:
            ok = all(s == "ok" for s, _ in results.values())
            user.save(last_run=datetime.now().strftime("%d %b %I:%M %p"), last_result="ok" if ok else "warning",
                      last_report=report)
        self.send(uid, report, summary=summary_line(results, dry_run))

    def _run_everyone(self, slot: str) -> None:
        users = [u for u in all_users(self.root) if u.status == "active"]
        log.info("Slot %s: updating %d user(s)", slot, len(users))
        random.shuffle(users)
        for u in users:
            time.sleep(random.randint(0, self.jitter_s))
            self._run_user(u.id, dry_run=False)

    # --- Scheduler --------------------------------------------------------------------

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

    # --- Helpers -------------------------------------------------------------------------

    def _help(self, p: str, is_admin: bool) -> str:
        lines = ["<b>Naukri profile bot</b>",
                 f"I refresh your Naukri profile at {self._hours_text()} IST every day so it shows as recently "
                 "updated - using only what's already on your profile.", ""]
        lines += [f"{p}{c} - {d}" for c, d in MENU if c != "help"]
        if is_admin:
            lines += ["", "<b>Admin</b>", "/users - list users", "/approve &lt;id&gt; - approve a user",
                      "/deny &lt;id&gt; - reject a pending user", "/remove &lt;id&gt; - delete a user and their data"]
        return "\n".join(lines)

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

    def _status_text(self, user: User, p: str) -> str:
        labels = {"approved": f"Not connected - send {p}login", "active": "✅ Active",
                  "needs_login": f"🔒 Session expired - send {p}login", "paused": "⏸ Paused"}
        lines = [f"<b>Status:</b> {labels.get(user.status, user.status)}"]
        if user.meta.get("email_hint"):
            lines.append(f"<b>Naukri account:</b> {escape(user.meta['email_hint'])}")
        lines.append(f"<b>Mode:</b> {'custom rotation' if user.config_path.exists() else 'refresh (uses only your own profile)'}")
        lines.append(f"<b>Last update:</b> {user.meta.get('last_run', 'never')}"
                     + (" (⚠️ partial)" if user.meta.get("last_result") == "warning" else ""))
        if user.status == "active":
            lines.append(f"<b>Next update:</b> {self._next_slot():%a %I:%M %p}")
        return "\n".join(lines)


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}{'*' * max(1, len(name) - 2)}@{domain}"
