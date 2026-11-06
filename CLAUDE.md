# CLAUDE.md

本文件是本仓库给 Claude Code（及协作者）的上下文文档：项目概览、开发约定与红线。

## 1. 项目概览

QQ 群运动数据机器人（NoneBot2 + OneBot v11）：Garmin / COROS 平台接入 + 截图 OCR 打卡 + 三榜排行播报 + 打卡体系 + 大模型增强层。MIT 开源，群专属内容全部 `.env` 配置化，可 fork 嫁接。

技术栈：Python 3.10+ · NoneBot2 · SQLAlchemy + SQLite · RapidOCR · OpenAI 兼容 LLM（默认智谱 GLM-5.3-Flash）· ruff / pytest。

## 2. 常用命令

```powershell
# 跑全量测试（务必用项目 .venv，不要用系统 python）
.venv\Scripts\python.exe -m pytest -q

# 静态检查与格式（提交前必须全过）
.venv\Scripts\ruff.exe check . --exclude .claude
.venv\Scripts\ruff.exe format --check app tests bot.py

# 本地启动（需先启动 NapCat 并扫码）
python bot.py
```

> 测试全部离线（不触网、不读真实 .env——`Settings(_env_file=None, ...)` 隔离）；新增配置项 / 服务函数时必须补单测。

## 3. 架构约定

- **分层**：`app/plugins`（消息收发 + 编排）→ `app/services`（业务逻辑，不依赖 nonebot 的 Bot/Event）→ `app/models`（ORM）。可复用逻辑一律下沉服务层。
- **LLM 只进增强层**：核心数据链路（OCR → 解析 → 落库 → 聚合 → 平台同步）全确定性代码；LLM 失败一律返回 `None`，调用方降级模板文案。
- **Session 纪律**：`Session` 非线程安全——`asyncio.to_thread` 内不共享 Session；线程里的阻塞 DB 调用各自开 session。
- **配置纪律**：任何群专属内容（机器人名 / 群号 / 活动 / 文案）**不得硬编码**，一律走 `app/config.py` 的 settings + `.env.example`；JSON 数组类配置要有「写错降级默认值」的容错。
- **调度纪律**：常驻循环（tasks.py）必须对未预期异常免疫（记 alert + 自动重试），定时任务不允许静默死亡。
- **幂等纪律**：打卡彩蛋 / 活动提醒等「只触发一次」的能力全部靠 `checkin_state` 表打标记。

## 4. 安全红线（任何改动必须遵守）

- 不得读取 / 暴露 `.env`；文档只以 `.env.example` 为模板。`.env`、`data/`、缓存目录已入 `.gitignore`。
- 成员账号密码绝不出现在群里：Garmin 必须私聊绑定，COROS 必须走官方 OAuth + PKCE（禁止用 App 的 API 密码方式登录）。
- 不打印 / 暴露 `passkey.json` 与 NapCat WebUI token。
- 发给 LLM 的只有聚合数字；截图上传受 `LLM_VISION_ENABLED` 控制；群成员提问进 LLM 前必须过 `guards` 清洗与注入拦截。
- `data/secret.key` 是 Fernet 密钥，丢失则全部凭据永久无法解密——文档与提示文案要持续提醒备份。

## 5. 关键路径速查

| 能力 | 入口 |
|---|---|
| 配置 | `app/config.py`（字段默认值 + 容错校验） |
| 截图打卡链路 | `app/plugins/image_ocr.py` → `services/parsers.py` → `services/sync.py` |
| 打卡体系 | `app/services/checkin.py`（顶部常量：里程碑 / 礼物 / 抽奖 / 节日） |
| 平台接入 | `app/services/providers/`（实现 `base.SportProvider` 即可加平台） |
| 排行 / 播报 | `app/services/ranking.py` + `services/tasks.py` |
| AI 增强 | `app/services/llm.py`（端点池故障转移 / 身份兜底） |
| 训练诊断 | `app/services/diagnose.py`（确定性公式，非 LLM） |
| 数据库迁移 | `app/db.py` 的 `_MIGRATIONS`（只 ADD COLUMN，幂等） |
| 运维告警 / 成员提醒 | `services/alert.py` / `services/notify.py` |
| 主机运维 / 掉线应急 | `local/nuc-runbook.md` + `local/nuc.py`（**本地私有，.gitignore 排除，不入库、不上传**） |
