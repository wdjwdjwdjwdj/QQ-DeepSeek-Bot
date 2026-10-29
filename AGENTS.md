# AGENTS.md — QQ-DeepSeek-Bot 项目规范

> 给 AI 助手与未来自己看的工程约定。改代码前先读完本文。

## 项目一句话

QQ 小号「大肥鱼」（鲸鱼娘人设）= MaiBot（LLM 框架）+ NapCat（QQ 协议端）+ DeepSeek/商汤/智谱 API，外加一组**计划任务驱动的根目录 Python 脚本**做守护、认知、记账、定时消息。

## 部署与代码同步（⚠️ 当前生产 = 华为云服务器）

> 2026-10 已整体迁移：生产运行在华为云 `1.95.124.70` 的 `/root/maibot/`（systemd 服务 + crontab，root SSH 密钥免密）。本机 Windows 目录只是开发副本，本机所有计划任务已禁用，**勿 re-enable（双端同时在线会互踢）**。下方"架构"一节保留的是本机历史拓扑；服务器上对应为 systemd + cron，根目录脚本是 **Linux 版（同名不同实现，以服务器版为准）**。

**代码真源 = GitHub 私有仓库（git 管理），禁止全量拷贝目录。** 仓库：`git@github.com:wdjwdjwdjwdj/QQ-DeepSeek-Bot.git`（私有，https://github.com/wdjwdjwdjwdj/QQ-DeepSeek-Bot）。服务器 `/root/maibot` 为生产基线（部署密钥 maibot-server 直推）；本机 clone 在 `C:\Users\29688\Desktop\脚本\QQ-DeepSeek-Bot-repo`（SSH 别名 `github.com-deepseek`，部署密钥 local-dev；旧目录 `QQ-DeepSeek-Bot\` 仅作历史存档，勿再改）。服务器与本地都是它的克隆：

1. **常规修改**：本地 clone 里改（直接告诉 AI 要改什么即可）→ `git add/commit/push` → 服务器 `cd /root/maibot && git pull` → 重启相关服务（systemd 单元需 restart；纯 cron 脚本下轮自动生效）。
2. **服务器热修**：可以在 `/root/maibot` 直接改文件，但改完必须**立刻** `git add/commit/push`，回到本地后先 `git pull`。禁止热修不回传——仓库必须始终是最新真源。
3. **双端纪律**：任何一端开工改代码前先 `git pull`；不要两头同时改同一文件。
4. **小改动可让 AI 经 SSH 直改服务器**并当场 commit；正式/批量改动走"本地改 → push → 服务器 pull"。

不进仓库（.gitignore；换机重建时需手工恢复）：

- 密钥：`napcat.env`、`my-maibot/config/model_config.toml`（所有 API key 都在这）
- 第三方运行包与登录态：`NapCat-OneKey/`、`onebot-qzone/`（第三方 Qzone 桥接，重建= `git clone https://github.com/Gu-Heping/onebot-qzone` → `npm install --registry=https://registry.npmmirror.com` → `npm run build`；服务器现存副本不受影响）、`my-maibot/` 整体忽略（仅白名单自研插件 `my-maibot/plugins/SchedulerManager/`、`my-maibot/plugins/FriendLedger/`、`my-maibot/plugins/VoiceSpeak/`）
- 运行时状态：`data/`、`logs/`、`*.log`、`*_state.json`、`schedule_config.json`、`friends.json`、`life_state.json`、`user_profile.json`、`wallet.json`、`__pycache__/`

仓库必须设为 **Private**。换机重建 = `git clone` + 手工恢复上述被忽略的密钥与状态文件。

## 架构（真实拓扑）

```
QQ ⇄ NapCat-OneKey（:3001 正向WS / :3002 HTTP API）
        ⇄ my-maibot（MaiBot 主程序，WebUI :8001）
              ├─ plugins/MaiBot-SnowLuma-Adapter   （第三方适配器，勿动）
              └─ plugins/SchedulerManager          （自研：LLM 工具，写 schedule_config.json）
              └─ plugins/VoiceSpeak               （自研：speak_voice 工具，edge-tts→NapCat 语音）

Windows 计划任务（每分钟级短进程，无长驻）：
  MaiBotWatchdog        PT2M   watchdog.py         全链路守护自愈（WAITING_QR_SCAN.flag 存在=等人工扫码，自动暂停 NapCat 拉起，扫码恢复自动清除）
  MaiBotCognition       PT10M  cognition.py        情绪/意图/矛盾/技能卡 + prompt 注入
  MaiBotTokenGuard      PT30M  token_guard.py      DeepSeek 余额求救
  MaiBotScheduledSender PT1M   scheduled_sender.py 读 schedule_config.json 发定时消息
  MaiBotWelcomeSender   PT2M   welcome_bot.py      新人入群欢迎：轮询群名单diff→智谱现写大肥鱼口吻欢迎语@新人(LLM挂回退随机池,首见群只记基线,--dry-run只打印不发)
  MaiBotPersonaAudit    每日03:00 persona_audit.py 人设漂移审查
  MaiBotDailyLife       每日05:00 daily_life.py     生活引擎：时间线/鱼粮/拍立得(已停用)/周观察
  MaiBotQzonePoster     每日19:40 qzone_poster.py   朋友圈：读post_draft经桥接发说说(只发文案,85%概率)
  MaiBotQzoneSocial     PT1H   qzone_social.py     Qzone社交：刷好友动态+关系分加权点赞+AI评论(拟人节律,每日赞≤12评≤4,键uin:tid幂等,--dry-run只读)
  MaiBotFriendsReview   每日04:45 friends_ledger.py 关系账本：滚动24h复盘调分(智谱免费,幂等,--dry-run可试跑)
  MaiBotVoiceTTS           按需    voice_tts.py       语音合成发送（edge-tts→NapCat；也可被 VoiceSpeak 插件调用）
  MaiBotSisterChat      每30分钟 sister_chat.py   随机主动找姐聊天：概率门控(预期3~5条/天)+世界观实况LLM现写消息
  MaiBotContextMemory   每12小时  context_memory.py  上下文半天归档：上一半天聊天→智谱压缩→data/memory_notes/日期.md(幂等键=半日键,LLM失败不推进键下轮重试,只读db)；半日token超上限(300万)在节内标⚠️+私聊哥一次(alerts键幂等)
  MaiBotDailyReport     每日21:00 token_guard.py --report  当日账单

ZCode 自动化（非 Windows 计划任务，迁移服务器后应删除）：
  night_watch.py        每30分钟 night_watch.py     兜底监控：只读体检+watchdog 失效代跑+当日 9 点发送验证；由 ZCode 自动化"每30分钟守护检查"调用，绝不发群消息

常驻服务（非计划任务，开机自启）：
  onebot-qzone/         QZone→OneBot v11 桥接（node dist/main.js，127.0.0.1:5700，
                        Cookie 已扫码登录并在 test_cache/ 保活；Startup/qzone_bridge_autostart.bat 自启，
                        端口被占自动退出；手动重启用 onebot-qzone/start_bridge.bat）
```

## 目录红线

| 位置 | 性质 | 规则 |
|---|---|---|
| 根目录 `*.py` | 自研脚本 | **文件名与位置不可移动/改名**——计划任务用绝对路径指向根目录，移动即断 |
| `botcommon.py` | 自研公共库 | 所有脚本的网络/落盘/key 读取必须走它 |
| `my-maibot/` | MaiBot 主程序 + 数据 | 只允许改 `config/*.toml`、`plugins/SchedulerManager/`、`plugins/FriendLedger/`、`plugins/VoiceSpeak/`；其余勿动 |
| `my-maibot/plugins/MaiBot-SnowLuma-Adapter/` | 第三方（12K 行） | **勿动** |
| `NapCat-OneKey/` | 第三方发行包 | **勿动** |
| `my-maibot/data/` | 记忆与状态 | 勿手动编辑；`*_state.json` 由脚本管理 |
| `cognition_state.json` 等 state 文件 | 脚本状态 | 勿手动编辑 |
| 编号 `0X-*.md` | 项目文档 | 改行为须同步更新对应文档 |

## 新增/修改脚本必须遵守

1. **文件头**：`# -*- coding: utf-8 -*-` + 中文 docstring，docstring 必须写明：功能、调用它的计划任务名与频率、用法示例、网络边界。
2. **网络**：一律 `botcommon.http_json(url, allowed, ...)`，白名单显式传参（如 `{"127.0.0.1": {"http"}}`），禁止裸 `urllib`/`requests` 出网；禁止跟随重定向。
3. **密钥**：只从 `my-maibot/config/model_config.toml` 读（`botcommon.load_provider_key`），任何文件不得硬编码 key。
4. **状态落盘**：一律 `botcommon.write_json_atomic(path, st)`（临时文件 + os.replace）；读回旧状态后增量修改，**禁止整体替换 dict 导致其他字段丢失**（token_guard 曾因此丢过余额轨迹）。
5. **崩溃边界**：主流程单点失败（LLM 超时、注入失败、发送失败）只跳过该步并 print，**state 必须照常保存**，必要时最后 `raise SystemExit(1)` 保留计划任务可见性；禁止让异常直接炸掉 main 导致 state 不落盘（cognition 曾因此重复评价消息）。
6. **守护类脚本的自愈逻辑必须闭环**：任何"拉起"分支都要有对应的"持续离线超时强制重拉"分支（watchdog 曾因缺这个分支对 MaiBot 永久沉默）。
7. **风格**：与现有脚本一致——中文注释、`Path` 处理路径、f-string、`secrets.choice` 选消息；不用第三方依赖（仅标准库 + maibot_sdk）。
8. **测试**：改 SchedulerManager 插件后必须跑 `py verify_scheduler.py`（自带配置快照恢复）；测试**绝不真发 QQ 消息**——`--test` 类参数只能用于用户明确要求端到端验证时人工执行。
9. **计划任务**：新增脚本后用 `schtasks /create` 注册（模板见 03-启动指南.md），并在本文"架构"一节登记任务名与频率。

## cognition.py 的 prompt 注入（特殊机制，改动须知）

- `inject_prompts()` 直接改写 `my-maibot/config/bot_config.toml` 中 `group_chat_prompt = ` 与 `private_chat_prompts = ` 两行（锚点行），以 `【认知引擎v1】` 首尾标记整段替换。
- **不可改这两行的键名**，不可改成多行 TOML 字符串（注入按"单行字符串右引号前插入"处理）。
- 写入流程：tmp 文件 → `tomllib` 解析验证 → `os.replace`，改动此函数必须保留这三步。

## 修改前的验证清单

```bash
py -m py_compile botcommon.py watchdog.py token_guard.py scheduled_sender.py cognition.py persona_audit.py  # 语法/导入
py verify_scheduler.py        # SchedulerManager 回归（含权限/校验/闭环 18 项断言）
py verify_botcommon.py        # 公共库单测（白名单拒绝/原子写/key 读取，不联网不发消息）
```

涉及守护/自愈逻辑的改动：先在纸上推演状态机（down_since / last_start / 冷却 / 超时重拉四态），确认每条转移都有出口，再动代码。
