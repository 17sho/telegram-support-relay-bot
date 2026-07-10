# Telegram Support Relay Bot

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
- 管理员消息撤回
- 首次 `/start` 使用说明及 `/help` 完整帮助
- 北京时间显示

## 准备工作

1. 在 [@BotFather](https://t.me/BotFather) 创建机器人并取得 Token。
2. 获取管理员自己的 Telegram 数字 ID，例如通过 [@userinfobot](https://t.me/userinfobot)。
3. Token、管理员 ID 和数据库都不要提交到 Git。

## 方案 A：服务器部署

需要 Python 3.11 或更高版本。

```bash
git clone <YOUR_REPOSITORY_URL>
cd telegram-support-relay-bot/server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```dotenv
BOT_TOKEN=你的BotFather令牌
ADMIN_IDS=你的Telegram数字ID
DEFAULT_VERIFY_INTERVAL_MINUTES=360
DATA_DIR=./data
```

支持多个管理员 ID，以英文逗号分隔。首次运行会自动创建空 SQLite 数据库：

```bash
.venv/bin/python bot.py
```

### systemd

按实际路径修改 `server/telegram-support-relay-bot.service`，然后：

```bash
sudo cp telegram-support-relay-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-support-relay-bot
sudo journalctl -u telegram-support-relay-bot -f
```

## 方案 B：Cloudflare Workers 部署

需要 Node.js、pnpm、Cloudflare 账号和 Wrangler 登录。

```bash
cd cloudflare-worker
pnpm install
pnpm wrangler login
pnpm wrangler d1 create telegram-support-relay-bot-db
cp wrangler.example.toml wrangler.toml
```

把创建 D1 后得到的 `database_id` 填入 `wrangler.toml`，再设置密钥：

```bash
pnpm wrangler secret put BOT_TOKEN
pnpm wrangler secret put ADMIN_IDS
pnpm run deploy
```

部署完成后，用浏览器访问以下地址一次以注册 Webhook：

```text
https://你的Worker域名/setup?secret=Bot_Token最后16个字符
```

成功时 Telegram 会返回 `Webhook was set`。数据库表会由 Worker 自动初始化。

本地开发可复制 `.dev.vars.example` 为 `.dev.vars`；该文件已被 `.gitignore` 排除。

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
