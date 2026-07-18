# Telegram Support Relay Bot

[简体中文](README.md) | [English](README_EN.md)

An open-source, two-way Telegram support and anonymous relay bot. Users send messages to the bot, the bot relays them to administrators, and administrators can respond by replying to a user card or by selecting a conversation.

This repository provides two alternative implementations:

- `server/`: Python + SQLite + long polling, suitable for a VPS, NAS, Raspberry Pi, or always-on computer.
- `cloudflare-worker/`: Cloudflare Workers + D1 + Webhook, suitable for low-maintenance serverless deployment.

> Do not run both editions with the same Bot Token. Telegram webhooks and long polling will conflict.

## Features

- **Private chats only:** even if the bot is added to a group as an administrator, it ignores group, supergroup, and channel messages; it neither relays them nor challenges group members
- Two-way relay for text, photos, videos, documents, voice messages, audio, and stickers
- Accurate reply and media mapping to prevent messages from reaching the wrong user
- Recent conversations, search, active conversation, and pending-message counts
- Paginated conversation history with media preview
- Block list and confirmation before unblocking
- Per-user CAPTCHA verification, verification intervals, and permanent exemptions
- Verification is suspended for 24 hours after two consecutive failed attempts
- Harder randomized challenges include three-number addition, subtraction, two-digit multiplication, mixed operations, number sequences, and second-largest-number selection with eight shuffled near-value choices
- Configurable per-user message rate limit (40 messages per minute by default); exceeding it requires human verification again
- Helps prevent message flooding and protects administrator chats and Cloudflare free-tier quotas
- Retraction of messages sent by an administrator
- First-run `/start` tutorial and complete `/help` reference
- Beijing time display

## Changelog

### 2026-07-18

- 🛡️ Fixed group messages being incorrectly detected and relayed after the bot was added to a group as an administrator.
- 🛡️ Fixed group members being incorrectly asked to complete human verification.
- Restricted commands, administrator replies, and user relay handling to private chats in the Python server edition.
- The Cloudflare Worker edition now immediately ignores group, supergroup, and channel messages.
- Existing private-chat relay, verification, and conversation-management behavior remains unchanged.
- Added CAPTCHA brute-force protection: two consecutive failures trigger a 24-hour lock; a successful verification clears the failure count.
- 🔐 Hardened human verification with six randomized challenge types: three-number addition, subtraction, single-digit multiplication, simple mixed operations, number sequences, and second-largest-number selection; two-digit multiplication is not used.
- 🔐 Increased answer choices from four to eight and replaced them with shuffled near-value distractors to reduce random-click and simple-script passes.
- ⏱️ Each challenge is valid for two minutes; an unanswered challenge older than two minutes counts as a failed attempt.

## Before You Deploy

### 1. Create a Telegram Bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot`.
3. Follow the prompts to choose a display name and a username ending in `bot`.
4. Save the Bot Token returned by BotFather. Treat it like a password: never share it or commit it to Git.
5. You should also complete the bot profile. Send `/mybots`, select your bot, and open **Edit Bot**:
   - **Edit Name**: sets the display name shown at the top;
   - **Edit Description**: sets the longer text under “What can this bot do?” on the profile page;
   - **Edit About**: sets the short About text;
   - **Edit Botpic**: uploads the bot profile picture;
   - **Edit Username**: changes the username ending in `bot`.

For example, you can enter this under **Edit Description**:

```text
📬 Contact via this Bot | 请用此 Bot 联系
🔗 https://t.me/your_bot_username
⚠️ No direct DMs from strangers, auto-archived
⚠️ 非熟人勿直接私信，否则自动归档
```

You may alternatively send `/setdescription`, `/setabouttext`, and `/setuserpic` directly to BotFather. `/setdescription` controls the “What can this bot do?” section of the Telegram bot profile.

If the Token is exposed, use `/revoke` in BotFather immediately to invalidate it.

### 2. Find the Administrator's Numeric Telegram ID

Open [@userinfobot](https://t.me/userinfobot), send any message, and record the numeric ID it returns. You need the numeric ID, not an `@username`.

### 3. Choose a Deployment Method

| Edition | Best for | Public domain required | Database | Always-on process |
| --- | --- | --- | --- | --- |
| Python server | VPS, NAS, Raspberry Pi, always-on computer | No | Local SQLite | Yes |
| Cloudflare Worker | Serverless and low-maintenance hosting | Cloudflare provides HTTPS | D1 | No |

> **Choose only one edition.** Never run both with the same Bot Token.
>
> Prefer a browser-only setup? See the detailed **[Cloudflare Dashboard deployment guide](docs/CLOUDFLARE_DASHBOARD_EN.md)**. [中文版](docs/CLOUDFLARE_DASHBOARD_ZH.md) is also available.

---

## Option A: Python Server Edition

The commands below target Ubuntu/Debian. Python 3.11 or newer is required, and the host must be able to reach the Telegram API.

### A1. Install System Packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
python3 --version
```

Upgrade Python first if the reported version is lower than 3.11.

### A2. Download the Project and Install Dependencies

```bash
cd /opt
sudo git clone https://github.com/17sho/telegram-support-relay-bot.git
sudo chown -R "$USER":"$USER" /opt/telegram-support-relay-bot
cd /opt/telegram-support-relay-bot/server

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### A3. Create the Configuration File

```bash
cp .env.example .env
nano .env
```

Set the following values:

```dotenv
BOT_TOKEN=your-token-from-BotFather
ADMIN_IDS=your-numeric-Telegram-ID
DEFAULT_VERIFY_INTERVAL_MINUTES=360
MESSAGES_PER_MINUTE=40
DATA_DIR=./data
```

Configuration reference:

- `BOT_TOKEN`: the complete Token from BotFather.
- `ADMIN_IDS`: numeric administrator ID. Separate multiple IDs with commas, for example `123456789,987654321`.
- `DEFAULT_VERIFY_INTERVAL_MINUTES`: how long verification remains valid; the default is `360` minutes.
- `MESSAGES_PER_MINUTE`: per-user message allowance per minute, default `40`; non-exempt users must verify again after exceeding it.
- `DATA_DIR`: SQLite storage directory. Keeping `./data` is recommended.

Restrict access to the configuration:

```bash
chmod 600 .env
```

### A4. Test in the Foreground

```bash
.venv/bin/python bot.py
```

After `starting relay bot` appears:

1. Send `/start` to the bot from the administrator account. The tutorial should appear.
2. Start the bot from a second Telegram account and complete verification.
3. Send text or a photo from the test account. The administrator should receive a user card.
4. Reply to that card from the administrator account. The test account should receive the reply.

Press `Ctrl+C` to stop the foreground process. The first run automatically creates `data/relay.db`; no manual schema setup is required.

### A5. Enable the systemd Service

The included service file assumes `/opt/telegram-support-relay-bot/server`. If you use another location, update `WorkingDirectory`, `EnvironmentFile`, and `ExecStart` first.

```bash
cd /opt/telegram-support-relay-bot/server
sudo cp telegram-support-relay-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-support-relay-bot.service
```

Check status and logs:

```bash
sudo systemctl status telegram-support-relay-bot.service --no-pager
sudo journalctl -u telegram-support-relay-bot.service -f
```

Common maintenance commands:

```bash
# Restart
sudo systemctl restart telegram-support-relay-bot.service

# Stop
sudo systemctl stop telegram-support-relay-bot.service

# Update
cd /opt/telegram-support-relay-bot
git pull
server/.venv/bin/pip install -r server/requirements.txt
sudo systemctl restart telegram-support-relay-bot.service
```

### A6. Back Up or Migrate the Database

User profiles and messages are stored in `server/data/relay.db`. Stop the service before copying it:

```bash
sudo systemctl stop telegram-support-relay-bot.service
cp /opt/telegram-support-relay-bot/server/data/relay.db ~/relay-backup.db
sudo systemctl start telegram-support-relay-bot.service
```

Never commit this database to a public repository.

---

## Option B: Cloudflare Workers Edition

This edition requires a Cloudflare account, Node.js 20 or newer, and pnpm. It uses a Telegram Webhook, and Cloudflare provides the public HTTPS endpoint.

### B1. Install Node.js and pnpm

Check your versions:

```bash
node --version
pnpm --version
```

If pnpm is missing:

```bash
corepack enable
corepack prepare pnpm@latest --activate
```

### B2. Download the Project and Sign In to Cloudflare

```bash
git clone https://github.com/17sho/telegram-support-relay-bot.git
cd telegram-support-relay-bot/cloudflare-worker
pnpm install
pnpm wrangler login
```

`wrangler login` opens a browser. Sign in to Cloudflare and authorize Wrangler.

### B3. Create the D1 Database

```bash
pnpm wrangler d1 create telegram-support-relay-bot-db
```

The result looks similar to:

```toml
[[d1_databases]]
binding = "DB"
database_name = "telegram-support-relay-bot-db"
database_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Copy the configuration template:

```bash
cp wrangler.example.toml wrangler.toml
nano wrangler.toml
```

Replace `REPLACE_WITH_YOUR_D1_DATABASE_ID` with the actual D1 ID. `wrangler.toml` contains an account resource identifier and is ignored by Git in this repository.

### B4. Configure Worker Secrets

Run:

```bash
pnpm wrangler secret put BOT_TOKEN
pnpm wrangler secret put ADMIN_IDS
```

Enter:

- `BOT_TOKEN`: the BotFather Token.
- `ADMIN_IDS`: the numeric administrator ID, or comma-separated IDs for multiple administrators.

Cloudflare stores these as encrypted secrets; they are not written to the project files.

### B5. Type-check and Deploy

```bash
pnpm run check
pnpm run deploy
```

Wrangler prints a URL similar to:

```text
https://telegram-support-relay-bot.<your-subdomain>.workers.dev
```

Check the health endpoint:

```bash
curl https://telegram-support-relay-bot.<your-subdomain>.workers.dev/health
```

Expected output:

```json
{"ok":true}
```

### B6. Register the Telegram Webhook

Use the last 16 characters of the Bot Token as the setup `secret`, and open this URL once:

```text
https://telegram-support-relay-bot.<your-subdomain>.workers.dev/setup?secret=<last-16-characters-of-token>
```

To avoid storing the URL in browser history, use a shell instead:

```bash
WORKER_URL='https://telegram-support-relay-bot.<your-subdomain>.workers.dev'
BOT_TOKEN='your-Bot-Token'
SETUP_SECRET="${BOT_TOKEN: -16}"
curl "$WORKER_URL/setup?secret=$SETUP_SECRET"
unset BOT_TOKEN SETUP_SECRET
```

The response should contain `"ok": true`. Verify the Webhook afterward:

```bash
curl "https://api.telegram.org/botYOUR_BOT_TOKEN/getWebhookInfo"
```

Confirm that:

- `url` points to your Worker `/webhook` endpoint;
- `pending_update_count` is not continuously increasing;
- `last_error_message` is empty.

Database tables are initialized automatically on the first request; no manual SQL migration is required.

### B7. Acceptance Test

1. Send `/start` from the administrator account; the full tutorial should appear on first use.
2. Start the bot from a second account and complete human verification.
3. Test text, photo, sticker, document, and voice relay.
4. Test administrator reply, conversation selection, history, and media preview.
5. Send an administrator message and test **Retract this message**.
6. Test blocking and the unblock confirmation flow.

### B8. Update, Inspect Logs, or Remove the Worker

```bash
# Update
cd telegram-support-relay-bot
git pull
cd cloudflare-worker
pnpm install
pnpm run check
pnpm run deploy

# Stream logs
pnpm wrangler tail

# Remove the Worker; this does not remove the D1 database
pnpm wrangler delete
```

Manage, back up, or delete D1 under **Workers & Pages → D1** in the Cloudflare dashboard. Deleting D1 permanently removes all conversation data.

### B9. Local Development (Optional)

```bash
cp .dev.vars.example .dev.vars
```

Fill `.dev.vars` with a test Token and administrator ID, then run:

```bash
pnpm wrangler dev
```

A local endpoint cannot receive Telegram Webhooks without an additional public HTTPS tunnel. Most users should test by deploying to Cloudflare instead.

---

## Troubleshooting

### The Bot Does Not Respond

- Confirm that the Token has no extra whitespace and has not been revoked.
- Confirm that `ADMIN_IDS` contains numeric IDs rather than usernames.
- For the server edition, inspect `journalctl`; for Worker, run `pnpm wrangler tail`.
- Confirm that the same Token is not running in both editions.

### `Conflict: terminated by other getUpdates request`

Another long-polling process is using the same Token. Stop the old process and leave only one server instance running.

### The Server Edition Stopped Receiving Messages After a Webhook Was Configured

Long polling and Webhooks cannot be used at the same time. Delete the old Webhook:

```bash
curl "https://api.telegram.org/botYOUR_BOT_TOKEN/deleteWebhook?drop_pending_updates=false"
```

Then restart the server service.

### Worker Deploys Successfully but Receives No Messages

Register the Webhook again and inspect `getWebhookInfo`. Also verify that `BOT_TOKEN`, `ADMIN_IDS`, and the D1 binding named `DB` are configured correctly.

### Change the Bot Token

- Server edition: update `.env` and restart the systemd service.
- Worker edition: run `pnpm wrangler secret put BOT_TOKEN`, deploy, and register the Webhook again.

### Completely Remove User Data

Back up first, then delete the SQLite or D1 database. This cannot be undone. Do not remove the SQLite file while the bot process is running.

## Administrator Commands

- `/start`: status; shows the full tutorial on first use
- `/help`: show all help
- `/sessions` or `/list`: recent conversations
- `/search <query>`: search by user ID, username, or name
- `/select <user ID>`: select an outgoing conversation
- `/history <user ID>`: view conversation history
- `/blocked`: show blocked users
- `/block <user ID>`: block a user
- `/unblock <user ID>`: unblock a user

## Security and Privacy

- This repository contains no Bot Token, real administrator ID, Cloudflare account data, or user database.
- `.env`, `.dev.vars`, `wrangler.toml`, D1 state, SQLite databases, and build directories are ignored.
- A person deploying this project controls the data collected by their bot and should provide an appropriate privacy notice and retention policy under applicable law.
- `/setup` uses the final 16 Token characters as a setup password. Do not share the setup URL. Production operators may call Telegram `setWebhook` directly and remove this route.

## Tests

```bash
# Python
python -m py_compile server/bot.py

# Worker
cd cloudflare-worker
pnpm install --frozen-lockfile
pnpm run check
```

## License

This project is released under the **MIT License**.

You may:

- use it for personal or commercial purposes;
- copy, modify, merge, and publish the source;
- distribute original or modified versions;
- include it in proprietary products.

You must preserve the original copyright and MIT license notices when using or distributing substantial portions of the software. The software is provided “as is,” without warranty.

See [`LICENSE`](LICENSE) for the full text.

---

If this project helps you, please consider giving it a ⭐ **Star** on GitHub. Your support helps more people discover the project and encourages continued maintenance and improvement!
