# Naukri Profile Refresh Agent

Every day at **09:00, 10:00, 11:00 and 12:00 IST** this agent logs in to Naukri and makes small, real edits so your
profile shows as recently updated:

| Section | What changes daily |
|---|---|
| Resume headline | Next variant from `config.yaml` (loops) |
| Key skills | Core skills stay; one skill from `rotation_pool` rotates out, the next rotates in |
| IT skills | One existing row (cycling through `it_skills.touch`) is opened and re-saved |

It runs on an **Oracle Cloud Always Free VM** (systemd timer), so your laptop can be off.
Results (and failure screenshots) go to **Telegram**, and if Naukri asks for an OTP the agent
messages you on Telegram and waits for you to reply with the code.

```
GitHub repo ──push──▶ GitHub Actions (validate + rsync over SSH)
                              │
                              ▼
Oracle VM (Ubuntu, Asia/Kolkata) ── systemd timer 09/10/11/12:00 ──▶ main.py run
  data/storage_state.json  (saved Naukri session)          │
  data/state.json          (rotation position)             ├─▶ naukri.com (headless Chromium)
  .env                     (credentials, chmod 600)        └─▶ Telegram (report / OTP)
```

---

## Multi-user Telegram bot (friends)

On the VM the agent runs as **`naukri-bot.service`**, which replaces the old timer:
one always-on process that answers Telegram messages and updates every active user
at the hours in `schedule.run_hours` (one browser at a time).

| Who | How it updates the profile |
|---|---|
| Admin (you, `TELEGRAM_CHAT_ID`) | *Custom rotation*: your `config.yaml` lists (copied in with `main.py adopt-me`) |
| Friends | *Refresh mode*: only their own profile is used - headline parts re-ordered (title stays first), one of their own key skills removed and re-added (cancelled without saving if re-adding fails), one of their own IT skills rows re-saved |

**Onboarding a friend:** they open the bot → `/start` → you get a message → `/approve <id>` →
they `/login` and send their Naukri email and password. The password message is deleted
from the chat right away, used once to log in, and never stored. Only the browser
session is kept, encrypted with `SESSION_KEY`, in `data/users/<chat_id>/session.enc`.
If Naukri asks for an OTP, the bot asks them for it. When a session expires they get
a message to `/login` again.

User commands: `/login /status /dryrun /runnow /pause /resume /delete /cancel /help`
Admin commands: `/users /approve <id> /deny <id> /remove <id>`

```bash
sudo systemctl status naukri-bot            # is it running?
journalctl -u naukri-bot -n 100 --no-pager  # what it did
sudo systemctl restart naukri-bot           # after deploying new code
```

### WhatsApp (optional, alongside Telegram)

Friends without Telegram can use WhatsApp via Meta's official **WhatsApp Cloud API**.
Same commands, with or without the slash (`status`, `login`, `dryrun`…).

- **Login:** `login` sends a one-time link (valid 15 min, single use) to a small page served
  by the bot, because the Cloud API cannot delete a password typed into the chat. OTPs are
  replied to in the WhatsApp chat.
- **24 h rule:** WhatsApp only allows free-form messages within 24 h of the user's last
  message. Outside that window the bot sends the approved utility template `naukri_update`
  with a one-line summary, and the full report is shown when they reply `report`.
- **Needs:** a public HTTPS address. `bash deploy/setup_https.sh <name>.duckdns.org`
  installs Caddy (free Let's Encrypt certificate), opens ports 80/443 on the VM and
  prints the webhook URL + verify token for Meta. Also add ingress rules for TCP 80/443
  in the Oracle security list.
- **.env:** `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`,
  `WHATSAPP_VERIFY_TOKEN`, `PUBLIC_BASE_URL`. If any are missing, the bot runs Telegram-only.

Template to create in WhatsApp Manager (category **Utility**, language **English**, name `naukri_update`):

```
Naukri profile bot: {{1}}
```

Capacity: about 1 minute per user per run, so the 1 GB VM is fine for roughly 15-20 users.
Many accounts logging in from one server IP can get flagged by Naukri, so keep it to a
small group of friends.

---

## 1. Local setup (Windows laptop) – do this first

```powershell
cd D:\Naukri-Agent
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m playwright install chromium
copy .env.example .env      # then fill in .env
```

### Telegram bot (2 minutes)
1. In Telegram, open **@BotFather** → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot.
3. `.\.venv\Scripts\python main.py telegram-test` → prints your `TELEGRAM_CHAT_ID`; put it in `.env`.
4. Run `telegram-test` again → you should receive a test message.

### Log in once and save the session
```powershell
.\.venv\Scripts\python main.py login            # uses NAUKRI_EMAIL / NAUKRI_PASSWORD from .env
.\.venv\Scripts\python main.py login --manual   # or log in yourself in the browser window
```
This writes `data/storage_state.json`.

### Calibrate and test
```powershell
.\.venv\Scripts\python main.py validate                     # check config, see next plan
.\.venv\Scripts\python main.py run --dry-run --headed       # watch it open each editor, saves nothing
.\.venv\Scripts\python main.py run --headed --no-jitter     # first real update
```
If a step fails, look in `logs/<date>/` for the screenshot + HTML, or run
`main.py inspect` to dump the profile and editor HTML to `logs/inspect/`.

---

## 2. Oracle Cloud VM

### Create the account
1. Sign up at <https://signup.cloud.oracle.com>. A card is needed for verification only;
   Always Free resources are not charged.
2. **Home region** can't be changed later – pick **India West (Mumbai)** or **India South (Hyderabad)**.
3. Recommended: upgrade the account to **Pay As You Go** (Billing → Upgrade). You still pay ₹0 while
   staying within Always Free limits, but Oracle stops reclaiming "idle" Always Free VMs – and this
   VM is idle 23h59m a day.

### Create the VM
Compute → Instances → **Create instance**
- **Image:** Canonical Ubuntu 24.04
- **Shape:** `VM.Standard.A1.Flex` (Ampere) 1 OCPU / 6 GB – or `VM.Standard.E2.1.Micro` if A1 says
  "out of capacity" (setup script adds swap for the 1 GB micro).
- **Networking:** default VCN, *assign a public IPv4 address*.
- **SSH keys:** *Generate a key pair* → download the private key (e.g. `oracle.key`).
- Create, then copy the **Public IP**.

### Connect and install
From your laptop (PowerShell):
```powershell
icacls oracle.key /inheritance:r /grant:r "$($env:USERNAME):R"   # ssh requires a private key file
ssh -i oracle.key ubuntu@<PUBLIC_IP> "mkdir -p ~/naukri-agent"
scp -i oracle.key -r main.py naukri config.yaml requirements.txt .env.example deploy ubuntu@<PUBLIC_IP>:~/naukri-agent/
scp -i oracle.key .env ubuntu@<PUBLIC_IP>:~/naukri-agent/.env
ssh -i oracle.key ubuntu@<PUBLIC_IP> "mkdir -p ~/naukri-agent/data"
scp -i oracle.key data/storage_state.json ubuntu@<PUBLIC_IP>:~/naukri-agent/data/
ssh -i oracle.key ubuntu@<PUBLIC_IP>
```
On the VM:
```bash
cd ~/naukri-agent
chmod 600 .env
bash deploy/setup_vm.sh                     # timezone, swap, Python, Chromium, systemd timer
.venv/bin/python main.py run --dry-run      # test from the VM
```

### Day-to-day commands on the VM
```bash
systemctl list-timers naukri-agent.timer    # next run time
sudo systemctl start naukri-agent           # run right now
journalctl -u naukri-agent -n 100           # last run output
ls logs/                                    # logs + failure screenshots
```

---

## 3. GitHub repo + auto-deploy

1. Create a **private** GitHub repo and push this folder (`.env`, `data/`, `logs/` are git-ignored).
2. Repo → Settings → Secrets and variables → Actions → add:
   - `ORACLE_HOST` – VM public IP
   - `ORACLE_USER` – `ubuntu`
   - `ORACLE_SSH_KEY` – full contents of `oracle.key`
3. Every push to `main` now validates `config.yaml`, syncs the code to the VM and restarts
   `naukri-bot`. Until these secrets exist, the deploy job is skipped rather than failing.

Your own rotation lists live in `data/users/<your chat id>/config.yaml` on the VM (copied
there by `main.py adopt-me`). To change them, edit that file on the VM; the repo's
`config.yaml` is the template and holds the schedule.

---

## Notes
- **Session expiry:** when the saved session expires the agent logs in with `.env` credentials. If
  Naukri asks for an OTP you get a Telegram message – reply with the code within 10 minutes.
  Captchas are never bypassed; you'll get an alert and can re-run `main.py login` on your laptop
  and copy `data/storage_state.json` to the VM again.
- **Bot protection:** Naukri sits behind Akamai, which blocks the default headless browser. The agent
  uses Chromium's full "new headless" mode with a normal user-agent. If Oracle's datacenter IP ever
  gets "Access Denied", the failure alert will say so.
- **Terms of service:** Naukri discourages automation. One gentle run per day with genuine edits from
  your own session keeps the risk low, but not zero.
