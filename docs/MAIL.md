# 邮件适配器使用说明

托盘菜单「邮箱账户…」添加账户。**授权码只在对话框里输入，存进 Windows 凭据管理器**
（控制面板 → 凭据管理器 → Windows 凭据，条目名 `DesktopAssistant/mail/<账户>/password`），
不进 config、不进 git、不经过聊天框。账户元数据在 `config/mail.json`（已 gitignore，模板 `mail.example.json`）。

## 各邮箱怎么拿授权码

| 邮箱 | 登录方式 | 获取方法 |
|---|---|---|
| QQ / Foxmail | 授权码 | 网页版 设置 → 账号 → 开启 IMAP/SMTP，短信验证后得到 16 位授权码 |
| 163 / 126 / yeah | 授权码 | 设置 → POP3/SMTP/IMAP → 开启 IMAP/SMTP 服务 → 授权码（客户端自动发 IMAP ID，避开 Unsafe Login） |
| Gmail | 应用专用密码（或 OAuth） | 开两步验证 → myaccount.google.com/apppasswords 生成 16 位密码 |
| Outlook / Hotmail / Live | 仅 OAuth2 | 微软 2024-09 起停用密码登录，见下 |
| Office 365（学校/企业） | 仅 OAuth2 | 同下；学校租户可能要求管理员同意第三方应用 |
| 新浪 / 阿里 / iCloud | 授权码 / App 专用密码 | 各自设置页开启 IMAP |
| 其他 | 手动填 host:port | 选「其他」 |

## OAuth2（Outlook / Office365 / Gmail 可选）

需要一个应用 ID，填在 `config/mail.json` 的 `oauth` 段：

**微软**（免费）：portal.azure.com → Microsoft Entra ID → 应用注册 → 新注册
- 受支持的账户类型：「任何组织目录中的账户和个人 Microsoft 帐户」
- 身份验证 → 高级设置 → 允许公共客户端流：是
- API 权限 → 添加 → 我的组织使用的 API 搜 "Office 365 Exchange Online" → 委托权限
  `IMAP.AccessAsUser.All`、`SMTP.Send`（再加 Microsoft Graph 的 `offline_access`）
- 把「应用程序(客户端) ID」填到 `oauth.microsoft.client_id`；只登学校邮箱可把 tenant 设成学校域名

然后在对话框选中账户点「微软/Google 登录」，按提示在浏览器打开网址输入设备码。
refresh_token 存凭据管理器，之后自动续期。**若学校/企业租户拦截未审核应用，需要找 IT 管理员放行。**

**Google**：console.cloud.google.com 建项目 → 启用 Gmail API → OAuth 同意屏幕（测试模式，把自己加为测试用户）
→ 凭据 → OAuth 客户端 ID，类型「桌面应用」→ client_id / client_secret 填到 `oauth.google`。
（Gmail 用应用专用密码更简单，推荐。）

## 功能与指令

- 后台每 `poll_min` 分钟收取（只读、BODY.PEEK，**不会把邮件标成已读**），未读总数写 `data/status.json` 的
  `unread_mail`，新邮件冒气泡 + 托盘通知（首次同步不提醒，避免刷屏）。
- `/邮件`、`/邮件 全部`、`/邮件 刷新`、`/邮件 看 序号`、`/邮件 已读 序号`、`/邮件 搜 关键词`、
  `/邮箱`、`/邮箱 测试 序号`、`/回复 序号 [正文]`、`/草稿`、`/发送 编号`、`/丢弃 编号`
- 大模型工具：`list_mail`、`read_mail`、`draft_mail`。**模型没有发信工具**：它只能起草，
  发出必须用户输入 `/发送 编号`。
- 国内邮箱 SMTP 发出的信不会自动进"已发送"，客户端会用 IMAP APPEND 补存一份（按 `\Sent` 标记找文件夹）。

### 已读 / 搜索 / 回复

- `/邮件 已读 序号`：序号取自最近一次 `/邮件`、`/邮件 全部` 或 `/邮件 搜` 列出的顺序。真正在 IMAP
  服务器上打 `\Seen` 标记（不再是只读 PEEK），同时同步本地缓存和 `unread_mail` 未读数，联网操作
  在后台线程执行，完成后通过气泡/面板回报。
- `/邮件 搜 关键词`：只在本地缓存（发件人、主题、摘要）里搜，不联网，搜不到就提示试试 `/邮件 刷新`
  先更新缓存。
- `/回复 序号 [正文]`：基于同一份"最近列出"的序号生成回复草稿——收件人取原邮件发件人地址，
  主题自动补 `Re:`（已经是 `Re:` 开头不会重复加），带 `In-Reply-To`（原邮件 Message-ID），
  正文后面附一段引用的原文摘要。正文留空时草稿里会是"（请补充回复内容）"占位，每次 `/回复` 都会
  生成一份新草稿（不会覆盖旧的），不满意可以 `/丢弃 编号` 旧的再回复一次。和 `/草稿` 生成的草稿
  一样，**仍然只是草稿**，确认无误后要输入 `/发送 编号` 才会真正发出。

## 代码结构（app/mail/）

providers（服务器预设）→ accounts（config/mail.json）→ credentials（凭据管理器，ctypes 直调 advapi32）
→ oauth（设备码 / 回环 PKCE，XOAUTH2）→ client（IMAP/SMTP）→ service（轮询、SQLite 缓存 mail_cache、草稿 mail_drafts）
→ commands（/ 指令）。测试：`python -m unittest discover tests`（假 IMAP/SMTP，不联网）。
