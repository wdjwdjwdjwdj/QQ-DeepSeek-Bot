# DSH 鲸鱼娘全家桶调研（2026-10-24 · 7天内热点）

> 你说的东西全部找到了，拼图如下。来源均见文末链接。

## 一、你提到的名词全部对上号了

| 你说的 | 实际是什么 |
|---|---|
| "DeepSeek harness 插件项目" | **DSH = DeepSeek Harness**，DeepSeek 官方开源 Agent 运行时（github.com/deepseek-ai/deepseek-harness，绰号"小黑鲸"，7 天内爆火，生态 700+ 插件） |
| "大飞鱼" | **dshgirl-starter** 插件里的**桌面大肥鱼/鲸鱼娘桌宠**，外加右下角"小飞鱼"余额挂件（仓库 FlyingFreeOwl/dshgirl-starter） |
| "人格卡·鲸鱼娘" | dshgirl-starter 的 PERSONA_LOAD 人设注入（鲸鱼娘形象来自 B 站 UP 主 ZipZipPipe 的表情包，15 张状态图可换角色） |
| "DeepSeek 全局接管 QQ 小号" | **dsh-qq-bridge**（TomoyoNatsume/dsh-qq-bridge）：QQ ↔ NapCat/OneBot ↔ DSH Agent 双向打通；另有 dsh-qq-plugin（xcisxc29）、dsh-im（xmanrui，9 种 IM）等同类 |
| "发表情包" | **dsh-meme** 插件（B 站有"鲸鱼娘会唱歌发表情包"演示） |
| "发朋友圈" | ⚠️ **目前 DSH 生态没有成熟的朋友圈插件**（最接近 dsh-im / dsh-qq-plugin，关注后续更新） |

## 二、完整架构（与我们已有设施的关系）

```
手机 QQ ←── 小号2365712359 ←── NapCat(已部署✅, 正向WS:3001✅, 自动登录✅)
                                        │
                ┌───────────────────────┼──────────────────────┐
          现方案：MaiBot              新方案：DSH 全家桶          DSH 桌面侧
          (已配商汤✅拟人闲聊)    dsh-qq-bridge 接 QQ 消息    dshgirl-starter
                                  dsh-meme 表情包            大肥鱼桌宠+小飞鱼
                                                             长期记忆 memory.json
```

**NapCat 层完全复用**——DSH 插件走的也是 OneBot 11 正向 WS，同一个 3001 即可（但 MaiBot 和 DSH 不能同时应答同一个号，要选一个接）。

## 三、关键事实与限制（决策前必读）

1. **DSH 支持商汤**：官方文档 providers.zh.md 支持"添加模型提供商"（自定义 OpenAI 兼容端点），`https://token.sensenova.cn/v1` 可配进去（有腾讯云完整教程）。
2. **⚠️ dsh-qq-bridge 的自动安装向导只适配 Linux/WSL2，原生 Windows 暂未适配**（README 明确写 Windows 用户建议 WSL2）。你是 Windows——要么 WSL2 跑 DSH（可连宿主机 NapCat），要么试 dsh-qq-plugin 等替代。
3. **定位差异（重要）**：dsh-qq-bridge 的定位是"**QQ 遥控 Agent**"（发任务、收结果、定时提醒），**不是 MaiBot 那种拟人闲聊**——它是"帮你干活的鲸鱼娘"，不是"潜伏群里的群友"。要"人味儿闲聊"，MaiBot 仍然是最对口的。
4. **朋友圈**：无现成插件。NapCat 本身有部分 QZone 接口，理论上可自写 DSH 插件调 NapCat 发，但属于自研工作量。
5. DSH 环境要求：Node ≥22.19（你本机 24.15 ✅）；桌宠需 dshgirl-starter（node≥22.19、x64）。

## 四、三条可选路线

| 路线 | 组成 | 优点 | 代价 |
|---|---|---|---|
| **R1 全家桶** | DSH(WSL2) + dshgirl + dsh-qq-bridge + dsh-meme + 商汤 | 最新玩法、大飞鱼桌宠、记忆、表情包、QQ 遥控 | 要装 WSL2；QQ 聊天偏"下指令"不是闲聊；无朋友圈 |
| **R2 混合（推荐）** | MaiBot 管 QQ 拟人聊天（现有）+ DSH(Windows原生) + dshgirl 桌宠（本地玩） | 两个需求各自最优：群里人味儿闲聊=麦麦，桌面大飞鱼=DSH；DSH Windows 原生可用（只是 qq-bridge 要 WSL2，桌宠插件不需要） | 两套都跑，占内存；朋友圈仍无解 |
| **R3 纯 MaiBot** | 现有方案，把 bot_config 人格改成"鲸鱼娘" | 零新部署、Windows 原生、闲聊体验最好 | 没有桌宠/DSH 生态 |

**朋友圈**：三条路线都没有现成方案。想要只能：(a) 等生态出插件；(b) 用 NapCat 的 QZone 接口自写（MaiBot 插件或 DSH 插件形式，工作量 1-2 天级别）。

## 五、来源

- [DeepSeek Harness 主仓库](https://github.com/deepseek-ai/deepseek-harness) / [模型提供商官方文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/providers.zh.md)
- [dshgirl-starter（鲸鱼娘+大肥鱼桌宠）](https://github.com/FlyingFreeOwl/dshgirl-starter) / [夜雨飘零介绍文](https://blog.yeyupiaoling.cn/article/1788555575634)
- [dsh-qq-bridge（QQ 接管）](https://github.com/TomoyoNatsume/dsh-qq-bridge) / [使用博客](https://blog.yeyupiaoling.cn/article/1792545809982)
- [dsh-meme 表情包演示（B站）](https://www.bilibili.com/video/BV17y8V61ETd)
- [鲸鱼娘形象来源（知乎）](https://zhuanlan.zhihu.com/p/2071930944357979616) / [DSH 生态报道](https://hub.baai.ac.cn/view/57162)
- [dsh-im 多渠道插件](https://github.com/xmanrui/dsh-im) / [dsh-qq-plugin](https://github.com/xcisxc29/dsh-qq-plugin)
- [DSH 配第三方 API 教程（腾讯云）](https://cloud.tencent.com/developer/article/2732777)
