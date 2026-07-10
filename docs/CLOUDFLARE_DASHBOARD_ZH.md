# Cloudflare 网页版部署指南（无需本地命令行）

[简体中文](CLOUDFLARE_DASHBOARD_ZH.md) | [English](CLOUDFLARE_DASHBOARD_EN.md) | [返回中文首页](../README.md)

本指南介绍如何只使用 **GitHub 网页**和 **Cloudflare Dashboard 网页**部署 Worker 版。整个过程不需要在电脑上安装 Node.js、pnpm 或 Wrangler。

> Cloudflare 控制台的菜单名称可能随版本略有变化。通常位于 **Workers & Pages**、**D1 SQL Database**、**Settings**、**Bindings** 和 **Variables and Secrets**。

## 准备事项

你需要：

- 一个 GitHub 账号；
- 一个 Cloudflare 账号；
- 通过 [@BotFather](https://t.me/BotFather) 创建的 Telegram Bot Token；
- 管理员自己的 Telegram 数字 ID，可通过 [@userinfobot](https://t.me/userinfobot) 获取。

不要把 Bot Token 写入 GitHub 文件。Token 只应填写到 Cloudflare 的加密 Secret 中。

## 第 1 步：Fork 项目

1. 打开项目：<https://github.com/17sho/telegram-support-relay-bot>。
2. 点击右上角 **Fork**。
3. 保持默认仓库名，点击 **Create fork**。
4. 后续操作均在你自己的 Fork 中完成。

Fork 的好处是 Cloudflare 可以从你的仓库自动构建，后续也能在 GitHub 网页中更新配置。

## 第 2 步：在 Cloudflare 网页创建 D1 数据库

1. 登录 [Cloudflare Dashboard](https://dash.cloudflare.com/)。
2. 在左侧打开 **Workers & Pages**。
3. 进入 **D1 SQL Database**（有时显示为 **D1**）。
4. 点击 **Create database**。
5. 数据库名称填写：

   ```text
   telegram-support-relay-bot-db
   ```

6. 点击 **Create**。
7. 进入刚创建的数据库，在详情页复制 **Database ID**。它是类似下面格式的 UUID：

   ```text
   xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
   ```

项目会在第一次收到请求时自动创建表，无需在 D1 控制台手工执行 SQL。

## 第 3 步：在 GitHub 网页填写 D1 ID

项目包含网页部署专用配置：

```text
cloudflare-worker/wrangler.dashboard.toml
```

操作步骤：

1. 打开你 Fork 后的 GitHub 仓库。
2. 进入 `cloudflare-worker/wrangler.dashboard.toml`。
3. 点击右上角铅笔图标 **Edit this file**。
4. 找到：

   ```toml
   database_id = "REPLACE_WITH_YOUR_D1_DATABASE_ID"
   ```

5. 将占位符替换为第 2 步复制的 D1 Database ID。
6. 点击 **Commit changes**，提交到 `main` 分支。

D1 Database ID 是资源标识，不是 Bot Token，但仍建议只在自己的部署仓库中维护。

## 第 4 步：从 GitHub 导入 Worker

1. 回到 Cloudflare Dashboard 的 **Workers & Pages**。
2. 点击 **Create application** 或 **Create**。
3. 选择 **Import a repository**、**Connect to Git** 或 **GitHub**。
4. 首次使用时授权 Cloudflare 访问 GitHub。
5. 选择你刚刚 Fork 的 `telegram-support-relay-bot` 仓库。
6. 按下面填写构建设置：

| 设置项 | 填写内容 |
| --- | --- |
| Project name | `telegram-support-relay-bot` |
| Production branch | `main` |
| Root directory | `cloudflare-worker` |
| Build command | `pnpm install --frozen-lockfile && pnpm run check` |
| Deploy command | `pnpm wrangler deploy --config wrangler.dashboard.toml` |

如果页面提供 **Build output directory**，请留空；Worker 项目不是静态 Pages 网站。

7. 点击 **Save and Deploy** 或 **Deploy**。
8. 等待构建完成。

部署成功后，Cloudflare 会显示类似地址：

```text
https://telegram-support-relay-bot.<你的子域>.workers.dev
```

如果构建失败，先检查：

- Root directory 是否为 `cloudflare-worker`；
- Deploy command 是否指定了 `wrangler.dashboard.toml`；
- D1 ID 是否已替换且没有多余空格；
- 你是否误选成了静态 Pages 项目。

## 第 5 步：确认 D1 Binding

正常情况下，`wrangler.dashboard.toml` 会自动创建名为 `DB` 的绑定。仍建议检查一次：

1. 打开刚部署的 Worker。
2. 进入 **Settings → Bindings**。
3. 应看到一个 **D1 database binding**：

| Variable name | Database |
| --- | --- |
| `DB` | `telegram-support-relay-bot-db` |

如果没有：

1. 点击 **Add binding**；
2. 类型选择 **D1 database**；
3. Variable name 填写 `DB`；
4. 选择刚创建的数据库；
5. 保存并重新部署。

变量名必须是大写的 `DB`，否则程序无法访问数据库。

## 第 6 步：添加普通变量和加密 Secret

打开 Worker 的 **Settings → Variables and Secrets**。

### 加密 Secret

添加以下两个变量，并将类型设置为 **Secret** 或点击 **Encrypt**：

| 名称 | 值 |
| --- | --- |
| `BOT_TOKEN` | BotFather 提供的完整 Token |
| `ADMIN_IDS` | 管理员数字 ID；多个 ID 用英文逗号分隔 |

### 普通变量

添加以下普通文本变量：

| 名称 | 默认值 | 说明 |
| --- | --- | --- |
| `DEFAULT_VERIFY_INTERVAL_MINUTES` | `360` | 默认验证有效时长，单位为分钟 |
| `MESSAGES_PER_MINUTE` | `40` | 每位非豁免用户每分钟消息上限 |

保存后，如果 Cloudflare 提示需要部署新版本，请点击 **Deploy**。不要把 `BOT_TOKEN` 设置成公开的纯文本 Variable。

## 第 7 步：检查 Worker 是否运行

在浏览器中访问：

```text
https://你的Worker域名/health
```

预期看到：

```json
{"ok":true}
```

如果出现 500 错误：

1. 进入 Worker；
2. 打开 **Logs** 或 **Observability → Logs**；
3. 检查是否缺少 `DB`、`BOT_TOKEN` 或 `ADMIN_IDS`；
4. 修正设置后重新部署。

## 第 8 步：注册 Telegram Webhook

Webhook 只需注册一次。取 Bot Token 的最后 16 个字符，访问：

```text
https://你的Worker域名/setup?secret=BotToken最后16个字符
```

例如，假设 Token 最后 16 个字符是 `abcdefghijklmnop`，则访问：

```text
https://你的Worker域名/setup?secret=abcdefghijklmnop
```

成功响应中应包含：

```json
{"ok":true}
```

注意：

- 不要把完整 Token 放进 URL；
- 不要截图或分享 setup 地址；
- 注册成功后无需重复访问；
- 如果更换 Token，需要更新 Secret 并重新注册 Webhook。

## 第 9 步：检查 Webhook 状态

在浏览器访问下面地址，把 `你的BotToken` 替换成完整 Token：

```text
https://api.telegram.org/bot你的BotToken/getWebhookInfo
```

确认：

- `url` 以你的 Worker 域名开头，并以 `/webhook` 结尾；
- `pending_update_count` 没有持续增加；
- `last_error_message` 为空或不存在。

检查完成后关闭该页面，避免 Token 长期留在浏览器历史记录中。如果担心泄露，可以在 BotFather 中撤销并重新生成 Token。

## 第 10 步：完整功能验收

建议准备管理员账号和另一个测试账号：

1. 管理员向机器人发送 `/start`，首次应显示完整教程。
2. 测试账号发送 `/start` 并完成人机验证。
3. 测试账号分别发送文字、图片、贴纸、文件和语音。
4. 管理员应收到对应消息卡片。
5. 管理员引用消息卡片回复，测试账号应收到回复。
6. 测试最近会话、搜索、历史记录和媒体预览。
7. 管理员发送一条消息后测试“撤回这条消息”。
8. 测试屏蔽、解除屏蔽和二次确认。
9. 如需测试限流，可暂时把 `MESSAGES_PER_MINUTE` 改成 `3`，确认第 4 条触发重新验证，测试后改回 `40`。

## 后续更新

Cloudflare 已连接你的 GitHub Fork。每次 `main` 分支有新提交时，Cloudflare Builds 会自动重新构建和部署。

要同步上游更新：

1. 打开你 Fork 的 GitHub 首页；
2. 点击 **Sync fork**；
3. 点击 **Update branch**；
4. Cloudflare 会自动触发新部署；
5. 在 Cloudflare 的 **Builds/Deployments** 页面确认结果。

如果上游更新与 `wrangler.dashboard.toml` 冲突，请保留你自己的 D1 Database ID。

## 查看日志与回滚

### 实时日志

1. 打开 Worker；
2. 进入 **Logs** 或 **Observability**；
3. 开启实时日志；
4. 在 Telegram 中重现问题；
5. 查看对应错误，但不要公开粘贴包含用户消息的完整日志。

### 回滚

1. 打开 Worker 的 **Deployments**；
2. 选择之前正常工作的版本；
3. 点击 **Rollback** 或 **Promote to production**。

回滚 Worker 代码通常不会回滚 D1 数据。数据库操作前应另行备份。

## 备份或删除 D1 数据

在 **D1 SQL Database** 中打开数据库，可以通过控制台查看表和执行查询。导出、备份和恢复功能以 Cloudflare 当前控制台为准。

删除数据库会永久删除所有用户和聊天记录。删除前请确认：

1. 已停止或删除 Worker；
2. 已完成必要备份；
3. 没有其他 Worker 绑定该数据库。

## 常见问题

### GitHub 仓库没有出现在 Cloudflare 列表中

在 GitHub 的应用授权页面允许 Cloudflare 访问该仓库，然后刷新 Cloudflare 导入页面。

### 构建提示找不到 `package.json`

Root directory 配置错误。应填写：

```text
cloudflare-worker
```

### 部署提示找不到 D1 数据库

检查 `wrangler.dashboard.toml` 中的 Database ID 是否属于当前 Cloudflare 账号，并确认数据库尚未被删除。

### Worker 健康检查正常，但 Telegram 没有消息

- 确认已注册 Webhook；
- 检查 `getWebhookInfo`；
- 确认 `BOT_TOKEN` 是 Secret 且内容完整；
- 确认 `ADMIN_IDS` 是数字 ID，不是 `@username`；
- 查看 Worker 实时日志。

### 修改变量后没有生效

Cloudflare 的变量变更通常需要生成新部署。保存变量后进入 **Deployments**，确认最新部署时间晚于变量修改时间。

### Webhook 报 401 或 setup 失败

确认 setup 参数是 Token 的最后 16 个字符，且 Cloudflare 中保存的 `BOT_TOKEN` 没有空格或换行。更换 Token 后必须重新注册 Webhook。

## 安全提醒

- Bot Token 只能存为 Cloudflare Secret；
- 不要把 Token、用户数据库或聊天日志提交到 GitHub；
- 不要公开 `/setup` 完整地址；
- GitHub Fork 建议开启依赖安全更新；
- 定期检查 Cloudflare 日志和 D1 使用量；
- 同一个 Token 不要同时运行服务器版和 Worker 版。

---

如果本指南对你有帮助，欢迎在 GitHub 给项目点一个 ⭐ **Star**！
