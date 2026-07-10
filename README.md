# Telegram Support Relay Bot

[简体中文](README.md) | [English](README_EN.md)

一个开源的 Telegram 双向客服/匿名中转机器人。普通用户给机器人发消息，机器人将消息转给管理员；管理员可以引用回复或选择会话后回复。

仓库同时提供两种实现，任选其一：

- `server/`：Python + SQLite + 长轮询，适合 VPS、NAS 或家用服务器。
- `cloudflare-worker/`：Cloudflare Workers + D1 + Webhook，适合无服务器部署。

> 两种实现不要同时连接同一个 Bot Token，否则 Telegram 更新可能被重复或竞争消费。

## 功能

- 文字、图片、视频、文件、语音、音频和贴纸双向转发
- 精确的引用回复和媒体映射，避免回复错人
- 最近会话、搜索、当前会话和未处理计数
- 会话记录分页及媒体预览
- 屏蔽名单和解除屏蔽二次确认
- 每个用户独立的人机验证、验证间隔和永久豁免
- 默认每位用户每分钟最多 40 条消息，可通过环境变量调整；超过限制后必须重新完成人机验证
- 有效减少恶意刷消息，保护管理员后台和 Cloudflare 免费额度
- 管理员消息撤回
- 首次 `/start` 使用说明及 `/help` 完整帮助
- 北京时间显示

## 部署前准备

### 1. 创建 Telegram Bot

1. 在 Telegram 中打开 [@BotFather](https://t.me/BotFather)。
2. 发送 `/newbot`。
3. 按提示设置机器人显示名称和以 `bot` 结尾的用户名。
4. 保存 BotFather 返回的 Bot Token。Token 相当于机器人密码，不要发给他人，也不要提交到 Git。
5. 可选：向 BotFather 发送 `/setdescription`、`/setabouttext` 和 `/setuserpic` 完善机器人资料。

如果 Token 泄露，请立即在 BotFather 中发送 `/revoke` 使旧 Token 失效。

### 2. 获取管理员数字 ID

在 Telegram 中打开 [@userinfobot](https://t.me/userinfobot) 并发送任意消息，记录返回的纯数字 ID。这里需要的是数字 ID，不是 `@username`。

### 3. 选择部署方式

| 方式 | 适合场景 | 需要公网域名 | 数据库 | 进程常驻 |
| --- | --- | --- | --- | --- |
| Python 服务器版 | VPS、NAS、树莓派、长期在线电脑 | 不需要 | 本地 SQLite | 需要 |
| Cloudflare Worker 版 | 无服务器、低维护部署 | Cloudflare 自动提供 | D1 | 不需要 |

> **只选择一种方式。** 不要让两个版本同时使用同一个 Bot Token，否则 Webhook 和长轮询会互相冲突。

---

## 方案 A：Python 服务器版

下面以 Ubuntu/Debian 为例。需要 Python 3.11 或更高版本，并且服务器可以访问 Telegram API。

### A1. 安装系统依赖

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
python3 --version
```

如果显示的 Python 低于 3.11，请先升级 Python。

### A2. 下载项目并安装依赖

```bash
cd /opt
sudo git clone https://github.com/17sho/telegram-support-relay-bot.git
sudo chown -R "$USER":"$USER" /opt/telegram-support-relay-bot
cd /opt/telegram-support-relay-bot/server

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### A3. 创建配置文件

```bash
cp .env.example .env
nano .env
```

填写以下内容：

```dotenv
BOT_TOKEN=在BotFather获得的Token
ADMIN_IDS=你的Telegram数字ID
DEFAULT_VERIFY_INTERVAL_MINUTES=360
MESSAGES_PER_MINUTE=40
DATA_DIR=./data
```

配置说明：

- `BOT_TOKEN`：BotFather 提供的完整 Token。
- `ADMIN_IDS`：管理员数字 ID。多个管理员使用英文逗号分隔，例如 `123456789,987654321`。
- `DEFAULT_VERIFY_INTERVAL_MINUTES`：用户验证有效时长，默认 `360` 分钟。
- `MESSAGES_PER_MINUTE`：每位非豁免用户每分钟允许的消息数，默认 `40`；超限后要求重新验证。
- `DATA_DIR`：SQLite 数据目录。建议保持 `./data`。

限制配置文件权限：

```bash
chmod 600 .env
```

### A4. 前台试运行

```bash
.venv/bin/python bot.py
```

看到 `starting relay bot` 后，在 Telegram 中：

1. 管理员向机器人发送 `/start`，应收到使用说明。
2. 用另一个 Telegram 账号向机器人发送 `/start` 并完成验证。
3. 测试账号发送文字或图片，管理员应收到转发卡片。
4. 管理员引用该卡片回复，测试账号应收到回复。

按 `Ctrl+C` 停止前台运行。首次运行会自动创建 `data/relay.db`，无需手工建表。

### A5. 配置 systemd 开机自启

仓库提供的服务文件默认使用 `/opt/telegram-support-relay-bot/server`。如果项目位于其他目录，需要先修改其中的 `WorkingDirectory`、`EnvironmentFile` 和 `ExecStart`。

```bash
cd /opt/telegram-support-relay-bot/server
sudo cp telegram-support-relay-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-support-relay-bot.service
```

检查状态和日志：

```bash
sudo systemctl status telegram-support-relay-bot.service --no-pager
sudo journalctl -u telegram-support-relay-bot.service -f
```

常用维护命令：

```bash
# 重启
sudo systemctl restart telegram-support-relay-bot.service

# 停止
sudo systemctl stop telegram-support-relay-bot.service

# 更新代码
cd /opt/telegram-support-relay-bot
git pull
server/.venv/bin/pip install -r server/requirements.txt
sudo systemctl restart telegram-support-relay-bot.service
```

### A6. 备份和迁移

用户资料和消息记录位于 `server/data/relay.db`。停止服务后复制该文件即可完整备份：

```bash
sudo systemctl stop telegram-support-relay-bot.service
cp /opt/telegram-support-relay-bot/server/data/relay.db ~/relay-backup.db
sudo systemctl start telegram-support-relay-bot.service
```

不要把数据库提交到公开仓库。

---

## 方案 B：Cloudflare Workers 版

需要 Cloudflare 账号、Node.js 20 或更高版本和 pnpm。Worker 使用 Telegram Webhook，Cloudflare 会自动提供 HTTPS 地址。

### B1. 安装 Node.js 和 pnpm

确认版本：

```bash
node --version
pnpm --version
```

如果尚未安装 pnpm：

```bash
corepack enable
corepack prepare pnpm@latest --activate
```

### B2. 下载项目并登录 Cloudflare

```bash
git clone https://github.com/17sho/telegram-support-relay-bot.git
cd telegram-support-relay-bot/cloudflare-worker
pnpm install
pnpm wrangler login
```

`wrangler login` 会打开浏览器，请登录 Cloudflare 并授权 Wrangler。

### B3. 创建 D1 数据库

```bash
pnpm wrangler d1 create telegram-support-relay-bot-db
```

命令会返回类似下面的内容：

```toml
[[d1_databases]]
binding = "DB"
database_name = "telegram-support-relay-bot-db"
database_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

复制配置模板：

```bash
cp wrangler.example.toml wrangler.toml
nano wrangler.toml
```

将 `database_id = "REPLACE_WITH_YOUR_D1_DATABASE_ID"` 替换为刚才获得的真实 D1 ID。`wrangler.toml` 含账户资源标识，默认不应提交到自己的公开分支。

### B4. 设置 Worker 密钥

依次执行：

```bash
pnpm wrangler secret put BOT_TOKEN
pnpm wrangler secret put ADMIN_IDS
```

每条命令都会要求输入值：

- `BOT_TOKEN`：粘贴 BotFather Token。
- `ADMIN_IDS`：输入管理员数字 ID；多个管理员用英文逗号分隔。

这些值会作为加密 Secret 保存在 Cloudflare，不会写入项目文件。

### B5. 类型检查和部署

```bash
pnpm run check
pnpm run deploy
```

部署完成后会显示 Worker 地址，例如：

```text
https://telegram-support-relay-bot.<你的子域>.workers.dev
```

先检查健康状态：

```bash
curl https://telegram-support-relay-bot.<你的子域>.workers.dev/health
```

预期返回：

```json
{"ok":true}
```

### B6. 注册 Telegram Webhook

取 Bot Token 的最后 16 个字符作为 `secret`，然后在浏览器访问一次：

```text
https://telegram-support-relay-bot.<你的子域>.workers.dev/setup?secret=<Token最后16个字符>
```

也可以使用命令行，避免把地址留在浏览器历史中：

```bash
WORKER_URL='https://telegram-support-relay-bot.<你的子域>.workers.dev'
BOT_TOKEN='你的Bot Token'
SETUP_SECRET="${BOT_TOKEN: -16}"
curl "$WORKER_URL/setup?secret=$SETUP_SECRET"
unset BOT_TOKEN SETUP_SECRET
```

返回内容中的 `ok` 应为 `true`。随后检查 Telegram Webhook：

```bash
curl "https://api.telegram.org/bot你的BotToken/getWebhookInfo"
```

确认：

- `url` 是你的 Worker `/webhook` 地址；
- `pending_update_count` 没有持续增长；
- `last_error_message` 为空。

数据库表会在第一次请求时自动初始化，无需手工执行 SQL。

### B7. 功能验收

1. 管理员发送 `/start`，第一次应显示完整教程。
2. 使用另一个 Telegram 账号启动机器人并完成人机验证。
3. 测试文字、图片、贴纸、文件和语音转发。
4. 测试管理员引用回复、选择会话、历史记录和媒体预览。
5. 发送一条管理员消息并测试“撤回这条消息”。
6. 测试屏蔽和解除屏蔽二次确认。

### B8. 更新、日志和删除部署

```bash
# 更新
cd telegram-support-relay-bot
git pull
cd cloudflare-worker
pnpm install
pnpm run check
pnpm run deploy

# 查看实时日志
pnpm wrangler tail

# 删除 Worker（不会自动删除 D1 数据库）
pnpm wrangler delete
```

在 Cloudflare 控制台的 **Workers & Pages → D1** 中可以查看、备份或删除数据库。删除 D1 会永久丢失所有会话数据。

### B9. 本地开发（可选）

```bash
cp .dev.vars.example .dev.vars
```

编辑 `.dev.vars` 填入测试 Token 和管理员 ID，然后运行：

```bash
pnpm wrangler dev
```

本地地址无法直接接收 Telegram Webhook，除非额外配置公网 HTTPS 隧道。普通使用者建议直接部署到 Cloudflare 测试。

---

## 常见问题

### 机器人没有回复

- 确认 Token 没有多余空格并且未被 BotFather 撤销。
- 确认管理员填写的是数字 ID，不是用户名。
- 服务器版检查 `journalctl`；Worker 版运行 `pnpm wrangler tail`。
- 确认同一 Token 没有同时运行服务器版和 Worker 版。

### 服务器版报 `Conflict: terminated by other getUpdates request`

同一个 Token 有另一个长轮询实例正在运行。停止旧进程，确保只运行一个服务实例。

### 服务器版不再收消息，但以前设置过 Webhook

长轮询和 Webhook 不能同时使用。删除旧 Webhook：

```bash
curl "https://api.telegram.org/bot你的BotToken/deleteWebhook?drop_pending_updates=false"
```

然后重启服务器服务。

### Worker 部署成功但收不到消息

重新执行 Webhook 注册，并通过 `getWebhookInfo` 检查错误。还要确认 Worker 的 `BOT_TOKEN`、`ADMIN_IDS` 和 D1 binding 名称 `DB` 均正确。

### 如何更换 Bot Token

- 服务器版：修改 `.env` 后重启 systemd 服务。
- Worker 版：重新运行 `pnpm wrangler secret put BOT_TOKEN`，部署后重新注册 Webhook。

### 如何完全清空用户数据

先备份，再删除 SQLite 数据库或 D1 数据库。此操作不可恢复。不要在机器人运行过程中直接删除 SQLite 文件。

## 管理员指令

- `/start`：状态；首次使用显示完整教程
- `/help`：查看全部帮助
- `/sessions` 或 `/list`：最近会话
- `/search <关键词>`：按用户 ID、用户名或姓名搜索
- `/select <用户ID>`：选中发送会话
- `/history <用户ID>`：查看会话记录
- `/blocked`：查看屏蔽名单
- `/block <用户ID>`：屏蔽联系人
- `/unblock <用户ID>`：解除屏蔽

## 安全与隐私

- 本仓库不包含 Bot Token、真实管理员 ID、Cloudflare 账户信息或用户数据库。
- `.env`、`.dev.vars`、D1 配置、SQLite 数据库和本地构建目录均被忽略。
- 公开部署者是其机器人收集数据的控制者，应根据所在地法律向用户提供隐私说明并制定数据保留政策。
- `/setup` 使用 Token 后 16 位作为一次性配置口令；注册成功后不要分享该 URL。生产环境也可以直接调用 Telegram `setWebhook` 后移除此入口。

## 测试

```bash
# Python
python -m py_compile server/bot.py

# Worker
cd cloudflare-worker
pnpm install --frozen-lockfile
pnpm run check
```

## 开源协议

本项目采用 **MIT License** 开源。

你可以自由地：

- 使用本项目，包括个人用途和商业用途
- 复制、修改、合并和发布源代码
- 分发原版或修改后的版本
- 将本项目用于闭源产品

使用或分发时，必须保留原始版权声明和 MIT 许可声明。本项目按“原样”提供，作者不对使用本项目产生的损失承担责任。

完整协议见 [`LICENSE`](LICENSE)。
