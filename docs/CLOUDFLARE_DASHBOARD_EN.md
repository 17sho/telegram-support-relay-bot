# Cloudflare Dashboard Deployment Guide (No Local CLI Required)

[简体中文](CLOUDFLARE_DASHBOARD_ZH.md) | [English](CLOUDFLARE_DASHBOARD_EN.md) | [Back to English README](../README_EN.md)

This guide explains how to deploy the Worker edition using only the **GitHub website** and **Cloudflare Dashboard**. You do not need to install Node.js, pnpm, or Wrangler locally.

> Cloudflare may occasionally rename or reorganize Dashboard menus. The relevant sections are generally under **Workers & Pages**, **D1 SQL Database**, **Settings**, **Bindings**, and **Variables and Secrets**.

## Prerequisites

You need:

- a GitHub account;
- a Cloudflare account;
- a Telegram Bot Token created through [@BotFather](https://t.me/BotFather);
- the administrator's numeric Telegram ID, available from [@userinfobot](https://t.me/userinfobot).

Never write the Bot Token into a GitHub file. Store it only as an encrypted Cloudflare Secret.

## Step 1: Fork the Repository

1. Open <https://github.com/17sho/telegram-support-relay-bot>.
2. Click **Fork** in the upper-right corner.
3. Keep the default repository name and click **Create fork**.
4. Perform the following steps in your own Fork.

Using a Fork lets Cloudflare build from your repository automatically and allows you to update configuration through the GitHub website.

## Step 2: Create a D1 Database in Cloudflare Dashboard

1. Sign in to [Cloudflare Dashboard](https://dash.cloudflare.com/).
2. Open **Workers & Pages** in the sidebar.
3. Open **D1 SQL Database** or **D1**.
4. Click **Create database**.
5. Enter this database name:

   ```text
   telegram-support-relay-bot-db
   ```

6. Click **Create**.
7. Open the new database and copy its **Database ID**. It is a UUID similar to:

   ```text
   xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ```

The application creates all database tables automatically on the first request. You do not need to run SQL manually in D1.

## Step 3: Add the D1 ID through GitHub

The repository includes a configuration file specifically for browser-based deployment:

```text
cloudflare-worker/wrangler.dashboard.toml
```

1. Open your Fork on GitHub.
2. Open `cloudflare-worker/wrangler.dashboard.toml`.
3. Click the pencil icon, **Edit this file**.
4. Find:

   ```toml
   database_id = "REPLACE_WITH_YOUR_D1_DATABASE_ID"
   ```

5. Replace the placeholder with the D1 Database ID copied in Step 2.
6. Click **Commit changes** and commit to `main`.

A D1 Database ID is a resource identifier rather than a Bot Token, but it is still best maintained in your own deployment Fork.

## Step 4: Import the Worker from GitHub

1. Return to **Workers & Pages** in Cloudflare Dashboard.
2. Click **Create application** or **Create**.
3. Choose **Import a repository**, **Connect to Git**, or **GitHub**.
4. If this is your first import, authorize Cloudflare to access GitHub.
5. Select your Fork of `telegram-support-relay-bot`.
6. Enter these build settings:

| Setting | Value |
| --- | --- |
| Project name | `telegram-support-relay-bot` |
| Production branch | `main` |
| Root directory | `cloudflare-worker` |
| Build command | `pnpm install --frozen-lockfile && pnpm run check` |
| Deploy command | `pnpm wrangler deploy --config wrangler.dashboard.toml` |

If Cloudflare asks for a **Build output directory**, leave it empty. This is a Worker, not a static Pages project.

7. Click **Save and Deploy** or **Deploy**.
8. Wait for the build to finish.

After a successful deployment, Cloudflare displays a URL similar to:

```text
https://telegram-support-relay-bot.<your-subdomain>.workers.dev
```

If the build fails, verify that:

- Root directory is `cloudflare-worker`;
- Deploy command references `wrangler.dashboard.toml`;
- the D1 ID was replaced correctly and has no extra whitespace;
- you did not accidentally create a static Pages project.

## Step 5: Verify the D1 Binding

`wrangler.dashboard.toml` should automatically create a binding named `DB`. Verify it once:

1. Open the deployed Worker.
2. Go to **Settings → Bindings**.
3. Confirm that this D1 database binding exists:

| Variable name | Database |
| --- | --- |
| `DB` | `telegram-support-relay-bot-db` |

If it is missing:

1. Click **Add binding**;
2. choose **D1 database**;
3. enter `DB` as the Variable name;
4. select the database created earlier;
5. save and redeploy.

The variable name must be uppercase `DB`; otherwise the Worker cannot access the database.

## Step 6: Add Variables and Encrypted Secrets

Open **Settings → Variables and Secrets** for the Worker.

### Encrypted Secrets

Add these two values and set their type to **Secret**, or click **Encrypt**:

| Name | Value |
| --- | --- |
| `BOT_TOKEN` | Complete Token provided by BotFather |
| `ADMIN_IDS` | Numeric administrator ID; separate multiple IDs with commas |

### Plain Variables

Add these plain-text variables:

| Name | Default | Description |
| --- | --- | --- |
| `DEFAULT_VERIFY_INTERVAL_MINUTES` | `360` | Default verification validity in minutes |
| `MESSAGES_PER_MINUTE` | `40` | Per-minute limit for each non-exempt user |

If Cloudflare requests a new deployment after saving, click **Deploy**. Never store `BOT_TOKEN` as a visible plain-text Variable.

## Step 7: Check Worker Health

Open this URL in a browser:

```text
https://your-worker-domain/health
```

Expected response:

```json
{"ok":true}
```

If you receive an HTTP 500 error:

1. open the Worker;
2. go to **Logs** or **Observability → Logs**;
3. check for a missing `DB`, `BOT_TOKEN`, or `ADMIN_IDS` setting;
4. correct the setting and deploy again.

## Step 8: Register the Telegram Webhook

You only need to register the Webhook once. Take the final 16 characters of the Bot Token and open:

```text
https://your-worker-domain/setup?secret=LAST_16_TOKEN_CHARACTERS
```

For example, if the Token ends in `abcdefghijklmnop`, open:

```text
https://your-worker-domain/setup?secret=abcdefghijklmnop
```

The successful response should contain:

```json
{"ok":true}
```

Important:

- never put the complete Token in the setup URL;
- do not screenshot or share the setup URL;
- you do not need to open it again after successful registration;
- after changing the Token, update the Secret and register the Webhook again.

## Step 9: Verify Webhook Status

Open this URL after replacing `YOUR_BOT_TOKEN` with the complete Token:

```text
https://api.telegram.org/botYOUR_BOT_TOKEN/getWebhookInfo
```

Confirm that:

- `url` begins with your Worker domain and ends in `/webhook`;
- `pending_update_count` is not continuously increasing;
- `last_error_message` is empty or absent.

Close the page afterward so the Token does not remain visible in browser history. If exposure is a concern, revoke and regenerate the Token through BotFather.

## Step 10: Complete an Acceptance Test

Use the administrator account and a separate test account:

1. Send `/start` from the administrator account. The full tutorial should appear on first use.
2. Send `/start` from the test account and complete human verification.
3. Send text, a photo, sticker, document, and voice message from the test account.
4. Confirm that the administrator receives the corresponding user cards.
5. Reply to a user card and verify that the test account receives the response.
6. Test recent conversations, search, history, and media preview.
7. Send a message from the administrator and test **Retract this message**.
8. Test blocking, unblocking, and the confirmation step.
9. To test rate limiting, temporarily set `MESSAGES_PER_MINUTE` to `3`. Confirm that the fourth message requires verification again, then restore it to `40`.

## Updating the Deployment

Cloudflare is now connected to your GitHub Fork. Every new commit to `main` triggers an automatic Cloudflare build and deployment.

To synchronize upstream updates:

1. open the homepage of your GitHub Fork;
2. click **Sync fork**;
3. click **Update branch**;
4. Cloudflare automatically starts a new deployment;
5. confirm the result under **Builds/Deployments** in Cloudflare.

If an upstream update conflicts with `wrangler.dashboard.toml`, preserve your own D1 Database ID.

## Logs and Rollback

### Live Logs

1. Open the Worker.
2. Go to **Logs** or **Observability**.
3. Start live logs.
4. Reproduce the problem in Telegram.
5. Inspect the error, but do not publicly share complete logs that contain user messages.

### Rollback

1. Open **Deployments** for the Worker.
2. Select a previously working version.
3. Click **Rollback** or **Promote to production**.

Rolling back Worker code normally does not roll back D1 data. Back up the database separately before database operations.

## Back Up or Delete D1 Data

Open the database under **D1 SQL Database** to view tables and run queries. Export, backup, and restore controls may change as Cloudflare updates its Dashboard.

Deleting the database permanently deletes every user profile and conversation. Before deletion, confirm that:

1. the Worker has been stopped or removed;
2. required backups have been completed;
3. no other Worker is bound to the database.

## Troubleshooting

### The GitHub Repository Is Missing from Cloudflare

Allow the Cloudflare GitHub application to access the repository, then refresh the import page in Cloudflare Dashboard.

### The Build Cannot Find `package.json`

The Root directory is incorrect. Set it to:

```text
cloudflare-worker
```

### Deployment Cannot Find the D1 Database

Verify that the Database ID in `wrangler.dashboard.toml` belongs to the current Cloudflare account and that the database still exists.

### Worker Health Is OK but Telegram Messages Do Not Arrive

- Confirm that the Webhook was registered;
- inspect `getWebhookInfo`;
- confirm that `BOT_TOKEN` is a Secret and contains the complete Token;
- confirm that `ADMIN_IDS` contains numeric IDs rather than `@usernames`;
- inspect live Worker logs.

### Variable Changes Do Not Take Effect

Cloudflare variable changes generally require a new deployment. After saving, open **Deployments** and confirm that the latest deployment is newer than the variable change.

### Webhook Returns 401 or Setup Fails

Confirm that the setup parameter is the final 16 Token characters and that `BOT_TOKEN` stored in Cloudflare has no spaces or line breaks. Register the Webhook again after changing the Token.

## Security Notes

- Store the Bot Token only as a Cloudflare Secret;
- never commit Tokens, user databases, or chat logs to GitHub;
- never publish the complete `/setup` URL;
- consider enabling dependency security updates on your GitHub Fork;
- inspect Cloudflare logs and D1 usage regularly;
- never run the server and Worker editions simultaneously with the same Token.

---

If this guide helped you, please consider giving the project a ⭐ **Star** on GitHub!
