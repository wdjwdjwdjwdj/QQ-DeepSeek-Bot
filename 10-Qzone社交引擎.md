# 10 — Qzone 社交引擎（刷动态/点赞/AI 评论）

> 2026-10-25 · 目标：大肥鱼"完全操控"自己的 Qzone——发朋友圈（已有 qzone_poster）之外，还会刷好友动态、点赞、留言。

## 能力全景（Q号社交闭环）

| 能力 | 实现 | 触发 |
|---|---|---|
| 发朋友圈 | qzone_poster.py → 桥接 `send_private_msg`（emotion_cgi_publish_v6） | 每日 19:40，85% 概率 |
| 刷好友动态 | qzone_social.py → 桥接 `get_friend_feeds`（feeds3_html_more） | 每小时 :10 |
| 点赞 | qzone_social.py → 桥接 `send_like`（internal_dolike_app，备选 like_cgi_likev6） | 同上，拟人节律 |
| 留言评论 | qzone_social.py → 桥接 `send_comment`（emotion_cgi_re_feeds），智谱 glm-5.3-flash 按人设生成 | 同上，35% 概率/轮 |

## qzone_social.py 工作流

1. 时段概率门：00-08 点绝不动作；8-12 点 45%、12-14 点 55%、14-18 点 45%、18-23 点 60%、23-24 点 30%；命中后随机延迟 0-4 分钟再动手。
2. 拉好友动态（40 条，快模式无图），过滤：自己的/EXCLUDE_QQ 的、36 小时外的旧帖、appid=217 活动流、已互动过的。
3. 按 friends.json 关系分加权挑人：重点朋友权重 6、聊得来 3、普通 1、爱答不理 0（不互动）、不在账本 1。
4. 每轮赞 1~4 个；35% 概率对一条**有文字的说说**生成评论（≤18 字口语，接内容不干夸），评论的帖子顺手也点赞。
5. 每日硬配额：赞 ≤12、评 ≤4；同一帖子终身只赞/评一次（state 键 `uin:tid`，7 天过期清理）。

## 状态与验证

- 状态：`qzone_social_state.json`（date/likes_today/comments_today/liked/commented/recent_comments），动作成功即落盘；日志 `qzone_social.log`。
- `--dry-run`：拉真实动态 + 选目标 + 生成评论，**只打印，不发送不落盘**（日常验证用）。
- `--force`：跳过时段概率强制一轮（配额/幂等仍生效；仅人工端到端验证时用，平时别开）。

## 服务器迁移注意（Linux）

- 计划任务改为 cron：`10 * * * * cd /path/to/QQ-DeepSeek-Bot && py qzone_social.py >> logs/cron_qzone_social.log 2>&1`（按现有 cron 风格调整）。
- 桥接用 `deploy/onebot-qzone.user.service.example` 起 systemd 用户服务。
- **Cookie 过期是常态**（skey 数天一死，p_skey 更长）：本机 2026-10-25 已遇一次，桥接会自动弹 Playwright 浏览器生成 `test_cache/qrcode.png` 等手机扫码。服务器上需 `QZONE_PLAYWRIGHT_HEADLESS=1`，扫码流程：`npm run login:qr` → 把 qrcode.png 拉回本地看/通过面板看 → 手机 QQ（2365712359）扫码。换服务器 IP 后首次大概率要重新扫码（IP 风控）。
- 新机器首次部署建议先 `py qzone_social.py --dry-run` 看一轮再放真动作。

## 已知边界

- 点赞/评论参数（unikey/curkey/appid）由桥接从 feeds3 缓存自动补全，脚本只传 `tid`/`user_id`/`abstime`/`appid`。
- 相册/网易云分享（appid 2/202）只点赞不评论；活动流（217）完全不碰。
- 评论生成失败只跳过评论，不降级为模板句（保人设质量）。
- 风控取舍：配额刻意保守。若真要放大，先观察一周 `qzone_social.log` 再调 `DAILY_LIKE_CAP`，一次别超过 ±50%。
