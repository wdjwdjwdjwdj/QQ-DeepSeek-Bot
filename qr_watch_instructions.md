# 夜间值班指令 v2（2026-10-25 07:22 更新，自动化每 30 分钟读取并执行）

项目：C:\Users\29688\Desktop\脚本\QQ-DeepSeek-Bot（大肥鱼=MaiBot+NapCat+计划任务守护链），用户当天白天迁移服务器。

绝对红线：测试/验证**绝不往任何群里发 QQ 消息**；私聊哥(2968874474)是唯一允许的发消息场景。
每个模式跑完把结论追加到 night_watch.log（一行），回复永远保持一行摘要。

## ⚠️ 等扫码特护模式（最高优先级，覆盖其余所有模式）

触发条件：night_watch.log 末尾包含「需要人工扫码登录」。
背景：07:05 NapCat 会话掉线，autoLogin 凭据过期落到扫码；watchdog 重复拉起堆了 28 个 QQ 进程已清光；MaiBotWatchdog 计划任务已被值班**故意禁用**（防止它拉新实例杀掉用户正要扫的码）；一个干净 NapCat 实例正在等扫码。

检查：跑 `py night_watch.py` 看 JSON 输出。

1. **napcat_session=false（仍在等扫码）**：本轮只做只读监控——把 `HH:MM 等待扫码中NapCat离线` 追加 night_watch.log，回复一行即止。
   **禁止**：跑 watchdog.py、taskkill 任何进程、拉起任何 QQ/NapCat 进程、启用 MaiBotWatchdog 任务（会杀掉用户正要扫的码）。跳过下面的 8 点/9 点分支修复动作。
2. **napcat_session=true（用户已扫码恢复）**：
   - 记 `HH:MM RECOVERED: 扫码恢复` 到 night_watch.log；
   - 跑 `schtasks /change /tn MaiBotWatchdog /enable` 恢复守护（Git Bash 里先 `export MSYS_NO_PATHCONV=1`），记入 log；
   - 3 分钟后跑一次 `py scheduled_sender.py` 确认宣讲会通知补发（once 窗口 11:00 前有效，state 键防重发），把结果记成 `HH:MM 宣讲会验证：<已发出/失败原因>`；
   - 若恢复时已过 11:00（once 已过期）且 state 无键：**17:00 前**跑 `py scheduled_sender.py --test AI宣讲会群通知` 补发（文案说"今晚 18:00 宣讲会"，17:00 前发出仍然真实有效），并私聊哥说明过期原因；**17:00 后**不补发，只私聊哥说明。

## 常规守护（特护模式不触发时执行）

cd 项目目录运行 `py night_watch.py`（只读体检，退出码 0=健康 1=异常，stdout 一行 JSON）。

- 退出码 0：把 `HH:MM verdict=OK` 追加 night_watch.log，回复一行摘要即止。
- 退出码 1，按 JSON 处理：
  - (a) actions 含 schtasks-enable-watchdog/watchdog-once = watchdog 计划任务曾失效且脚本已代跑自愈：等 2 分钟重跑复测，转绿记 `HH:MM verdict=RECOVERED`；
  - (b) bot_config.toml 解析失败或 scheduled_sender_state.json 损坏：不改任何配置/状态文件，详情记 night_watch.log 并用 botcommon.napcat_send_private(2968874474,'值班报告: <问题>') 私聊哥；
  - (c) NapCat/MaiBot 持续离线：再跑一次 `py watchdog.py`，3 分钟后复查，仍挂按 (b) 私聊。

## 08:25~08:59 追加体检（特护模式触发时跳过本节）

常规守护后加跑 `py night_watch.py --preflight`（只读）：

- napcat_session/maibot_webui=false → 跑 `py watchdog.py` 修复并复测；
- preflight 里"AI宣讲会群通知"的 bot_role 不是 admin → 私聊哥报警（@全体会失败）；
- 查 scheduled_sender_state.json 的 sent 是否含 "2026-10-25 08:30 早安问候"（8:30 私聊金丝雀，确认发送链路通）。
- 结论 `HH:MM preflight <结论>` 追加 night_watch.log 并回复。

## 09:00~10:55 发送验证（特护模式触发时跳过本节）

- 若 night_watch.log 已含「宣讲会验证」字样 → 只做常规守护；若已有「宣讲会验证：失败」→ 复查 state 键，已出现则补记「宣讲会验证：补发成功」。
- 否则读 scheduled_sender_state.json：sent 含 "once 2026-10-25 09:00 AI宣讲会群通知" → 成功，记 `HH:MM 宣讲会验证：已发出` 并回复。
- 不含键 → (1) 跑 `py scheduled_sender.py`（计划任务每分钟的常规发送路径，不是测试消息，状态键防重发，stdout 给真实错误）；(2) 失败原因是 NapCat/会话类 → 跑 `py watchdog.py` 修复，等 90 秒再跑一次 sender；(3) 权限/风控/管理员类 → 立即 botcommon.napcat_send_private(2968874474,'宣讲会通知发送失败: <错误>，需人工处理') 私聊哥。
- 结论必须含「宣讲会验证」字样记入 log 并回复（此标记防止后续轮次重复验证）。

## 10:55 后补验（特护模式触发时跳过本节）

若 night_watch.log 无「宣讲会验证」行：用只读 API `http://127.0.0.1:3002/get_group_msg_history?group_id=1125309087&count=50`（必要时用返回末条 message_seq 翻页）查 09:00 前后机器人 2365712359 是否发过含"宣讲会"的群消息 → 记 `HH:MM 宣讲会验证：<已发出/未发出-过期>`，未发出则私聊哥说明。之后照常常规守护。

## 迁移完成后

用户迁服务器后：删除本自动化 + 删除本文件 + 删除 night_watch.py（或在 AGENTS.md 里改状态）。
