"""Messaging channels for the bot: Telegram (long polling) and WhatsApp Cloud API (webhook).

User ids: Telegram users are their plain chat id ("6380311280"), WhatsApp users are
"wa-<phone number>" ("wa-919812345678").
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from html import escape, unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs

import requests

log = logging.getLogger(__name__)

# on_message(user_id, display_name, text, message_ref)
OnMessage = Callable[[str, str, str, Optional[str]], None]


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str):
        self.token = token

    def owns(self, uid: str) -> bool:
        return not uid.startswith("wa-")

    def api(self, method: str, http_timeout: int = 20, files=None, **data) -> dict:
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", data=data, files=files,
                          timeout=http_timeout)
        if not r.ok:
            log.warning("Telegram %s failed: %s", method, r.text[:200])
            return {}
        return r.json()

    def send(self, uid: str, html: str) -> None:
        for i in range(0, len(html), 3900):
            self.api("sendMessage", chat_id=uid, text=html[i : i + 3900], parse_mode="HTML",
                     disable_web_page_preview="true")

    def send_photo(self, uid: str, path: Path, caption: str) -> None:
        if path.exists():
            with path.open("rb") as f:
                self.api("sendPhoto", chat_id=uid, caption=caption[:1000], files={"photo": f})

    def delete(self, uid: str, ref: Optional[str]) -> None:
        if ref:
            self.api("deleteMessage", chat_id=uid, message_id=ref)

    def set_commands(self, menu: list[tuple[str, str]]) -> None:
        self.api("setMyCommands", commands=json.dumps([{"command": c, "description": d} for c, d in menu]))

    def poll_forever(self, on_message: OnMessage) -> None:
        offset = None
        while True:
            try:
                data = {"timeout": 25, **({"offset": offset} if offset else {})}
                updates = self.api("getUpdates", http_timeout=35, **data).get("result", [])
            except requests.RequestException as e:
                log.warning("Telegram polling failed: %s", e)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if msg.get("chat", {}).get("type") != "private" or "text" not in msg:
                    continue
                frm = msg.get("from", {})
                name = " ".join(filter(None, [frm.get("first_name"), frm.get("last_name")])) or str(msg["chat"]["id"])
                try:
                    on_message(str(msg["chat"]["id"]), name, msg["text"], str(msg["message_id"]))
                except Exception:
                    log.exception("Handling Telegram message failed")


def html_to_whatsapp(html: str) -> str:
    """Convert the Telegram-HTML reports to WhatsApp formatting."""
    text = re.sub(r"</?b>", "*", html)
    text = re.sub(r"</?i>", "_", text)
    text = re.sub(r"</?code>", "`", text)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text)


class WhatsAppChannel:
    """WhatsApp Cloud API. Free-form messages only reach users who messaged us in the
    last 24 h; outside that window we must send the pre-approved utility template."""

    name = "whatsapp"

    def __init__(self, token: str, phone_number_id: str, app_secret: str, verify_token: str,
                 base_url: str, template: str, template_lang: str, port: int, api_version: str):
        self.token = token
        self.phone_id = phone_number_id
        self.app_secret = app_secret.encode()
        self.verify_token = verify_token
        self.base_url = base_url.rstrip("/")
        self.template = template
        self.template_lang = template_lang
        self.port = port
        self.graph = f"https://graph.facebook.com/{api_version}"

    def owns(self, uid: str) -> bool:
        return uid.startswith("wa-")

    @staticmethod
    def number(uid: str) -> str:
        return uid.removeprefix("wa-")

    def _post(self, path: str, **kwargs) -> dict:
        r = requests.post(f"{self.graph}/{path}", headers={"Authorization": f"Bearer {self.token}"},
                          timeout=30, **kwargs)
        if not r.ok:
            log.warning("WhatsApp %s failed: %s", path, r.text[:300])
            return {}
        return r.json()

    def send(self, uid: str, html: str) -> None:
        text = html_to_whatsapp(html)
        for i in range(0, len(text), 4000):
            self._post(f"{self.phone_id}/messages", json={
                "messaging_product": "whatsapp", "to": self.number(uid), "type": "text",
                "text": {"body": text[i : i + 4000], "preview_url": False},
            })

    def send_template(self, uid: str, summary: str) -> None:
        # Template parameters may not contain newlines/tabs or long runs of spaces.
        param = re.sub(r"\s+", " ", summary).strip()[:900] or "update"
        self._post(f"{self.phone_id}/messages", json={
            "messaging_product": "whatsapp", "to": self.number(uid), "type": "template",
            "template": {"name": self.template, "language": {"code": self.template_lang},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": param}]}]},
        })

    def send_photo(self, uid: str, path: Path, caption: str) -> None:
        if not path.exists():
            return
        with path.open("rb") as f:
            media = self._post(f"{self.phone_id}/media", data={"messaging_product": "whatsapp", "type": "image/png"},
                               files={"file": (path.name, f, "image/png")})
        if media.get("id"):
            self._post(f"{self.phone_id}/messages", json={
                "messaging_product": "whatsapp", "to": self.number(uid), "type": "image",
                "image": {"id": media["id"], "caption": caption[:1000]},
            })

    def delete(self, uid: str, ref: Optional[str]) -> None:
        pass  # the Cloud API cannot delete a user's message - passwords go via the login page instead

    def login_url(self, token: str) -> str:
        return f"{self.base_url}/login/{token}"

    # --- Webhook + login page --------------------------------------------------

    def serve_forever(self, on_message: OnMessage, login_page: "LoginPage") -> None:
        channel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # keep request lines (they contain login tokens) out of logs
                pass

            def _reply(self, code: int, body: str, ctype: str = "text/plain") -> None:
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/whatsapp/webhook"):
                    q = parse_qs(self.path.partition("?")[2])
                    if q.get("hub.mode") == ["subscribe"] and q.get("hub.verify_token") == [channel.verify_token]:
                        return self._reply(200, q.get("hub.challenge", [""])[0])
                    return self._reply(403, "forbidden")
                if m := re.fullmatch(r"/login/([A-Za-z0-9_-]{20,})", self.path):
                    return self._reply(*login_page.form(m.group(1)), "text/html")
                if self.path == "/health":
                    return self._reply(200, "ok")
                self._reply(404, "not found")

            def do_POST(self):
                length = min(int(self.headers.get("Content-Length", 0)), 1_000_000)
                raw = self.rfile.read(length)
                if self.path.startswith("/whatsapp/webhook"):
                    sig = self.headers.get("X-Hub-Signature-256", "")
                    expected = "sha256=" + hmac.new(channel.app_secret, raw, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(sig, expected):
                        return self._reply(403, "bad signature")
                    self._reply(200, "ok")  # ack fast; Meta retries slow webhooks
                    threading.Thread(target=channel._dispatch, args=(raw, on_message), daemon=True).start()
                    return
                if m := re.fullmatch(r"/login/([A-Za-z0-9_-]{20,})", self.path):
                    form = parse_qs(raw.decode("utf-8", "replace"))
                    email = (form.get("email") or [""])[0].strip()
                    password = (form.get("password") or [""])[0]
                    return self._reply(*login_page.submit(m.group(1), email, password), "text/html")
                self._reply(404, "not found")

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        log.info("WhatsApp webhook + login page listening on 127.0.0.1:%d (public: %s)", self.port, self.base_url)
        server.serve_forever()

    def _dispatch(self, raw: bytes, on_message: OnMessage) -> None:
        try:
            payload = json.loads(raw)
            for entry in payload.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
                    for msg in value.get("messages", []):
                        if msg.get("type") == "text":
                            wa_id = msg["from"]
                            on_message(f"wa-{wa_id}", names.get(wa_id) or wa_id, msg["text"]["body"], msg.get("id"))
                        elif msg.get("type") == "button":  # quick-reply on a template
                            wa_id = msg["from"]
                            on_message(f"wa-{wa_id}", names.get(wa_id) or wa_id, msg["button"]["text"], msg.get("id"))
        except Exception:
            log.exception("Handling WhatsApp webhook failed")


class LoginPage:
    """One-time login links, so passwords are never typed into a chat that can't be deleted."""

    TTL = 15 * 60

    def __init__(self, on_submit: Callable[[str, str, str], None]):
        self.on_submit = on_submit  # (user_id, email, password)
        self.tokens: dict[str, tuple[str, float]] = {}
        self.lock = threading.Lock()

    def new_token(self, uid: str) -> str:
        import secrets

        token = secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            self.tokens = {t: v for t, v in self.tokens.items() if v[1] > now and v[0] != uid}
            self.tokens[token] = (uid, now + self.TTL)
        return token

    def _valid(self, token: str) -> Optional[str]:
        with self.lock:
            uid, expires = self.tokens.get(token, ("", 0))
        return uid if expires > time.time() else None

    def form(self, token: str) -> tuple[int, str]:
        if not self._valid(token):
            return 410, _page("Link expired", "<p>This login link has expired or was already used. "
                                              "Send <b>login</b> to the bot to get a new one.</p>")
        return 200, _page("Connect Naukri", f"""
<p>Enter your <b>Naukri</b> login. Your password is used once to sign in and is never stored -
only the login session is kept, encrypted.</p>
<form method="post" action="/login/{escape(token)}" autocomplete="off">
  <label>Email<input name="email" type="email" required autocapitalize="none"></label>
  <label>Password<input name="password" type="password" required></label>
  <button type="submit">Connect</button>
</form>
<p class="small">If Naukri sends you an OTP, the bot will ask for it on WhatsApp.</p>""")

    def submit(self, token: str, email: str, password: str) -> tuple[int, str]:
        with self.lock:
            uid, expires = self.tokens.pop(token, ("", 0))  # single use
        if not uid or expires < time.time():
            return 410, _page("Link expired", "<p>This link has expired. Send <b>login</b> to the bot for a new one.</p>")
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or not password:
            return 400, _page("Try again", "<p>Please go back and enter a valid email and password.</p>")
        self.on_submit(uid, email, password)
        return 200, _page("Connecting…", "<p>✅ Got it. Logging in to Naukri now - check WhatsApp for the result "
                                         "(and an OTP request, if Naukri asks for one). You can close this page.</p>")


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>{escape(title)}</title><style>
body{{font-family:system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 16px;color:#1d1d1f;background:#fafafa}}
h1{{font-size:22px}} label{{display:block;margin:14px 0;font-weight:600}}
input{{display:block;width:100%;box-sizing:border-box;padding:12px;margin-top:6px;font-size:16px;border:1px solid #ccc;border-radius:8px}}
button{{width:100%;padding:13px;font-size:16px;border:0;border-radius:8px;background:#4a6cf7;color:#fff;font-weight:600}}
.small{{color:#666;font-size:13px}}
</style></head><body><h1>{escape(title)}</h1>{body}</body></html>"""
