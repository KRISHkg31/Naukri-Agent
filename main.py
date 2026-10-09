"""Naukri profile refresh agent.

Commands:
  python main.py login              one-time: log in in a visible browser and save the session
  python main.py run [--dry-run]    the daily job (headline, key skills, IT skills)
  python main.py validate           check config.yaml and print today's plan
  python main.py check-skills      verify rotation_pool names exist in Naukri's skill catalog
  python main.py inspect            dump profile/editor HTML to logs/inspect for selector fixes
  python main.py telegram-test      send a test message / find your chat id
  python main.py bot                multi-user Telegram bot (friends) with the daily schedule built in
  python main.py adopt-me           move this single-user setup into the bot as the admin's account
  python main.py gen-key            generate SESSION_KEY (encrypts users' saved sessions)
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from naukri import sections
from naukri.report import format_report
from naukri.rotation import load_config, load_state, plan_today

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.yaml"
DATA_DIR = ROOT / "data"
SESSION_FILE = DATA_DIR / "storage_state.json"
STATE_FILE = DATA_DIR / "state.json"
LOG_DIR = ROOT / "logs"

log = logging.getLogger("naukri-agent")


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / f"run-{datetime.now():%Y-%m-%d}.log", encoding="utf-8"),
        ],
    )


def make_notifier():
    from naukri.notifier import Notifier

    return Notifier(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))


def cmd_validate(args) -> int:
    cfg = load_config(CONFIG_FILE)
    plan = plan_today(cfg, load_state(STATE_FILE))
    print("config.yaml OK\n\nNext run will do:\n" + plan.describe())
    return 0


def cmd_run(args) -> int:
    """Update this machine's own profile (data/ + config.yaml rotation lists)."""
    from naukri.runner import run_update

    cfg = load_config(CONFIG_FILE)
    notifier = make_notifier()
    tag = " [DRY RUN]" if args.dry_run else ""
    if not args.dry_run and not args.no_jitter:
        delay = random.randint(0, cfg["schedule"]["max_jitter_seconds"])
        log.info("Waiting %ss before starting", delay)
        time.sleep(delay)
    try:
        results, last_updated = run_update(
            SESSION_FILE,
            STATE_FILE,
            LOG_DIR,
            rotate_cfg=cfg,
            dry_run=args.dry_run,
            headless=not args.headed,
            only=set(args.only.split(",")) if args.only else None,
            email=os.getenv("NAUKRI_EMAIL"),
            password=os.getenv("NAUKRI_PASSWORD"),
            otp_provider=notifier.ask_otp,
            on_failure_shot=lambda shot, caption: notifier.send_photo(shot, caption),
        )
    except Exception as e:
        log.exception("Run failed")
        notifier.send(f"❌ Naukri agent{tag}: {e}")
        return 1
    notifier.send(format_report(results, args.dry_run, last_updated), html=True)
    return 0 if all(status == "ok" for status, _ in results.values()) else 1


def cmd_login(args) -> int:
    from naukri.browser import LOGIN_URL, NaukriBrowser

    email, password = os.getenv("NAUKRI_EMAIL"), os.getenv("NAUKRI_PASSWORD")
    with NaukriBrowser(SESSION_FILE, headless=False) as nb:
        if email and password and not args.manual:
            nb.login(email, password, otp_provider=lambda: input("Enter the OTP Naukri sent you: ").strip())
        else:
            nb.page.goto(LOGIN_URL)
            print("Log in to Naukri in the browser window (waiting up to 5 minutes)…")
            deadline = time.time() + 300
            while "nlogin" in nb.page.url and time.time() < deadline:
                nb.page.wait_for_timeout(2000)
            if not nb.open_profile():
                print("Did not reach the profile page - try again.")
                return 1
            nb.save_session()
    print(f"Session saved to {SESSION_FILE}")
    return 0


def cmd_inspect(args) -> int:
    """Save the profile page and each editor's HTML so selectors can be checked/fixed."""
    from naukri.browser import NaukriBrowser

    out = LOG_DIR / "inspect"
    out.mkdir(parents=True, exist_ok=True)
    with NaukriBrowser(SESSION_FILE, headless=not args.headed) as nb:
        if not nb.open_profile():
            print("Not logged in - run `python main.py login` first.")
            return 1
        (out / "profile.html").write_text(nb.page.content(), encoding="utf-8")
        nb.page.screenshot(path=str(out / "profile.png"), full_page=True)
        for title in ("Resume headline", "Key skills"):
            try:
                editor = sections.open_editor(nb.page, title)
                slug = title.lower().replace(" ", "_")
                (out / f"editor_{slug}.html").write_text(editor.evaluate("e => e.outerHTML"), encoding="utf-8")
                nb.page.screenshot(path=str(out / f"editor_{slug}.png"))
                print(f"✔ {title} editor opened")
            except Exception as e:
                print(f"✖ {title}: {e}")
            nb.open_profile()
    print(f"Saved to {out}")
    return 0


def cmd_check_skills(args) -> int:
    """Check that skill names exist in Naukri's catalog (Naukri rejects anything else)."""
    import yaml

    from naukri.browser import NaukriBrowser

    names = args.names or yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))["key_skills"]["rotation_pool"]
    with NaukriBrowser(SESSION_FILE) as nb:
        if not nb.open_profile():
            print("Not logged in - run `python main.py login` first.")
            return 1
        found = sections.check_skill_names(nb.page, names)
    bad = 0
    for name, items in found.items():
        exact = next((i for i in items if i.lower() == name.lower()), None)
        if exact:
            print(f"OK       {name}  (Naukri shows it as '{exact}')")
        else:
            bad += 1
            print(f"MISSING  {name}  -> suggestions: {', '.join(items[:6]) or 'none'}")
    return 1 if bad else 0


USERS_DIR = DATA_DIR / "users"


def _session_key() -> str:
    key = os.getenv("SESSION_KEY")
    if not key:
        raise SystemExit("SESSION_KEY missing in .env - generate one with `python main.py gen-key`.")
    return key


def cmd_bot(args) -> int:
    """Multi-user Telegram bot + built-in daily schedule (runs forever)."""
    from naukri.bot import Bot

    cfg = load_config(CONFIG_FILE)
    token, admin = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not admin:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (the admin) must be set in .env")
        return 1
    Bot(token, admin, USERS_DIR, _session_key(), cfg["schedule"]["run_hours"],
        cfg["schedule"]["max_jitter_seconds"], DATA_DIR / "bot_slots.json").run()
    return 0


def cmd_adopt_me(args) -> int:
    """Move the single-user setup (data/ + config.yaml) into the bot as the admin's account."""
    import shutil

    from naukri.store import PlainStore
    from naukri.users import User

    admin = os.getenv("TELEGRAM_CHAT_ID")
    user = User(USERS_DIR, admin)
    session = PlainStore(SESSION_FILE).load()
    if not session:
        print(f"No session at {SESSION_FILE} - run `python main.py login` first.")
        return 1
    if not user.exists:
        user.create("admin", "active")
    user.store(_session_key()).save(session)
    if STATE_FILE.exists():
        shutil.copy(STATE_FILE, user.state_path)
    user.save(status="active")
    print(f"Adopted into {user.dir} (session encrypted).")
    return 0


def cmd_gen_key(args) -> int:
    from naukri.store import new_key

    print(f"SESSION_KEY={new_key()}")
    return 0


def cmd_telegram_test(args) -> int:
    n = make_notifier()
    if not n.token:
        print("Set TELEGRAM_BOT_TOKEN in .env first.")
        return 1
    if not n.chat_id:
        chats = n.discover_chat_ids()
        if not chats:
            print("No chats found. Send any message to your bot in Telegram, then run this again.")
            return 1
        for cid, name in chats:
            print(f"TELEGRAM_CHAT_ID={cid}   ({name})")
        print("Put that value in .env and run this again.")
        return 0
    n.send("👋 Naukri agent test message - Telegram is working.")
    print("Sent.")
    return 0


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(ROOT / ".env")
    setup_logging()
    parser = argparse.ArgumentParser(description="Daily Naukri profile refresh agent")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the daily update")
    run.add_argument("--dry-run", action="store_true", help="open editors but don't save anything")
    run.add_argument("--headed", action="store_true", help="show the browser window")
    run.add_argument("--no-jitter", action="store_true", help="skip the random start delay")
    run.add_argument("--only", help="comma-separated subset of: headline,keyskills,itskills")
    run.set_defaults(func=cmd_run)

    login = sub.add_parser("login", help="log in once and save the session")
    login.add_argument("--manual", action="store_true", help="type credentials yourself in the browser")
    login.set_defaults(func=cmd_login)

    inspect = sub.add_parser("inspect", help="dump page HTML for selector debugging")
    inspect.add_argument("--headed", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    check = sub.add_parser("check-skills", help="verify skill names exist in Naukri's catalog (no saving)")
    check.add_argument("names", nargs="*", help="names to check (default: key_skills.rotation_pool)")
    check.set_defaults(func=cmd_check_skills)

    sub.add_parser("validate", help="check config and show next plan").set_defaults(func=cmd_validate)
    sub.add_parser("bot", help="multi-user Telegram bot with built-in schedule").set_defaults(func=cmd_bot)
    sub.add_parser("adopt-me", help="move this single-user setup into the bot as the admin").set_defaults(func=cmd_adopt_me)
    sub.add_parser("gen-key", help="generate a SESSION_KEY for .env").set_defaults(func=cmd_gen_key)
    sub.add_parser("telegram-test", help="test Telegram / find chat id").set_defaults(func=cmd_telegram_test)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
