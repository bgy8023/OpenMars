# 🚀 OpenMars V3.0 工业级网文创作系统

基于 Streamlit + OpenAI 兼容 SDK 的小说创作助手：单模型串行流水线（大纲师 → 排雷师 → 主笔）、SQLite 记忆宫殿、零 token 质量门禁、每章运行报告与移动端告警。

> 说明：当前版本为**单模型串行流水线**，不是多智能体并行架构。「主脑/写手双模型分层路由」为未来路线（详见 `.env.example` 末尾说明）；写后记忆抽取已支持配置独立的便宜模型（`LLM_EXTRACT_MODEL_NAME`）。

## ✨ 核心功能

### 📖 章节生成流水线（`openmars_core/query_engine.py`）
- **大纲师→排雷师→主笔三步串行**：每章依次生成剧情大纲（含「场景N-要点-字数」场景计划字段卡）、四类毒点排雷（人设崩塌/时间线矛盾/设定冲突/伏笔问题）、按场景配额写正文（`query_engine.py:328` `run_chapter_pipeline`）
- **长章逐段续写**：目标字数 ≥5000 字且场景数 ≥2 时，按场景配额逐段生成，每段携带已写全文尾部（≤2000 字），并做防复读修剪（`prompts.py:29` `LONG_CHAPTER_THRESHOLD`、`query_engine.py:81` `_trim_overlap`）；短章整章单次调用
- **调用健壮性**：同步 OpenAI 客户端（无异步上下文冲突）；限流尊重 `Retry-After` 头否则指数退避+抖动；4xx 客户端错误不重试；空响应额外重试一次；被 `max_tokens` 截断的响应带 `truncated` 标记并在日志告警（`query_engine.py:183` `call_llm_sync`）
- **提示词具名收口**：全部 prompt 模板集中在 `openmars_core/prompts.py`，引擎只渲染不内联拼接

### 🚦 质量门禁（`openmars_core/validators.py`，零 token 正则检查）
- **字数硬门禁**：交付字数必须落在 `[0.7×目标字数, 目标字数+10%]`；未提供目标字数时以 `MAX_CHAPTER_WORDS` 兜底（`validators.py:31`）
- **AI 味套话频次**：默认词表 8 条（如「瞳孔骤缩」「不是…而是」句式），白名单原则——单次命中不算，同一种累计超上限才判违规，绝不程序化改写正文
- **长段堆叠**：连续 3 段超过 300 字判违规（适配手机阅读）
- **对话比例**：可选门禁，默认关闭，可在 `novel_settings/<书>/05-质量规则.md` 开启；该文件还可覆盖套话词表与各阈值
- **番茄建议区间只是提示**：2500-4500 字区间仅产出报告级 `platform_hint`，绝不触发重写
- **门禁闭环**：`TOMATO_AUDIT_COMPATIBLE=true` 时，硬门禁不达标自动定向重写（违规清单回喂 prompt，最多 2 轮）；仍不达标则在交付正文尾部追加显式标注块【质量门禁告警·非正文】，绝不静默降级（`cyber_printer_ultimate.py:179` `_run_gate_loop`）

### 🔍 一致性审校（可选）
- `CONTENT_SAFETY_CHECK=true` 时，每章生成后追加一次独立审校调用：对照人物档案+伏笔台账+角色状态，输出连续性可疑点清单（如「角色第N章已死亡但本章出场」），写入运行报告供人工复核；关闭时零额外 LLM 调用（`validators.py:510` `run_consistency_review`）

### 🧠 记忆宫殿（`openmars_core/memory_palace.py`，SQLite Schema v2）
- **四张表**：`chapter_memory`（摘要/正文/大纲/排雷/结尾摘录）、`story_state`（滚动全局摘要等）、`foreshadow_ledger`（伏笔台账，只排不滤——超期条目标红保留，绝不静默删除）、`character_state`（角色状态）
- **WAL 模式 + 主键/索引查询**，面板读与后台线程写互不阻塞
- **旧 JSON 数据自动迁移**：首次初始化时把旧版 `03-动态剧情记忆.json` 导入 SQLite（原文件改名为 `.bak.migrated`）
- **生成上下文骨架统一装配**：五节固定注入（全局摘要≤2000字 → 最近3章摘要各≤200字 → 上一章结尾≤800字 → 未兑现伏笔Top8 → 活跃角色Top8），总预算 12000 字，超限显式截断标注（`memory_palace.py:249` `build_generation_context`）
- **写后回写闭环**：每章生成完毕追加一次结构化抽取调用（可用便宜模型），自动登记埋设/回收伏笔、更新角色状态与章节摘要；每 5 章或台账变动时滚动压缩全局摘要（≤2000字）；抽取/压缩失败显式告警并保留旧状态，绝不阻断章节交付（`cyber_printer_ultimate.py:99` `_writeback_memory`）
- **面板可手工编辑**：全局摘要、伏笔条目（内容/预计回收章/状态）均可编辑，编辑留痕 `source=user_edit`

### 📦 批量连跑·断点续跑
- 侧边栏设定起止章后串行连跑；`chapter_memory.full_content` 非空的章自动跳过，中途取消后再启动不重跑已完成章（`cyber_printer_ultimate.py:271` `generate_chapter_batch`）；每章正文落盘 `output/<书>/第N章_时间戳.md`

### 📊 生成过程全程可见
- **阶段化进度**：进度随流水线阶段推进（大纲 10→30、排雷 30→40、正文 40→95，长章段内线性细化），非匀速假进度
- **协作式取消**：面板「⏹ 停止生成」或任务超 2 小时置位取消事件，流水线只在步骤边界（含门禁重写轮之间）安全停止，绝不打断单步 LLM 调用中途、不作废已完成的调用
- **每章运行报告**：`output/<书>/第N章_报告.json` 收录三步输出摘要、门禁结果与重写轮数、一致性审校结果、本章 token/成本（配置单价时）、全部告警留痕；失败路径同样落报告
- **LLM 调用记账**：每次调用写一行进 `logs/cost.db`（含失败尝试）；面板「用量与成本」展示今日/累计 token 与估算成本；命令行日报 `python -m openmars_core.llm_ledger`

### 📱 移动端告警
生成开始/完成/失败/取消/门禁未通过/回写失败等关键事件推送到手机（Bark/Server酱/飞书/钉钉）；未配置 Webhook 时告警仍进运行报告留痕，不黑洞

### 🎨 Web 面板（`openmars_panel.py`）
- **4 个工作区**：✍️ 章节生成、📚 记忆宫殿（固定设定/上下文骨架预览/全局摘要/伏笔台账/章节历史）、📖 生成历史（章节 .md + 运行报告）、❓ 帮助说明
- **生成状态机**：生成中按钮锁定；生成任务在独立后台线程执行，与页面会话解耦——即使刷新页面，后台任务也会跑完并把章节落盘 `output/`（刷新后本页的进度与结果回显会丢失）
- **侧边栏**：小说项目切换、章节号/目标字数/自定义剧情、批量连跑、大模型配置（一键保存到 `.env`、🔌 测试连接实测 1-token 请求、创作温度滑块）、用量与成本

## 🚀 快速开始

### 方式一：本地运行

```bash
# 1. 克隆仓库
git clone https://github.com/bgy8023/OpenMars.git
cd OpenMars

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env 填写：LLM_API_KEY、LLM_BASE_URL、LLM_MODEL_NAME

# 3. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 4. 启动 Web 面板
streamlit run openmars_panel.py --server.port 8501
```

访问 http://localhost:8501 即可使用。macOS/Linux 也可执行 `./run_openmars.sh` 一键启动。

### 方式二：Docker 部署

```bash
# 先准备 .env（容器内以只读方式挂载）
cp .env.example .env && vim .env

# 构建并启动
docker-compose up -d --build

# 查看日志
docker-compose logs -f
```

访问 http://localhost:8501。`docker-compose.yml` 将 `novel_settings/`、`output/`、`logs/` 挂载到宿主机，`.env` 只读挂载——**请在宿主机编辑 .env，容器内「保存到.env」不可用**。

## 📝 小说设定文件

在 `novel_settings/<你的小说名>/` 目录下准备（参考 `novel_settings/模板/`）：

| 文件 | 作用 | 缺失时 |
|---|---|---|
| `00-全本大纲.md` | 全本剧情大纲 | 固定设定缺该节 |
| `01-人物档案.md` | 主角、配角人设与核心设定 | 同上 |
| `02-世界观设定.md` | 世界观、背景、规则 | 同上 |
| `03-番茄审核铁则.md` | 平台审核红线（推荐） | 自动使用内置兜底铁则（`prompts.py:288` `DEFAULT_TOMATO_RULES`），面板会提示补文件 |
| `05-质量规则.md`（可选） | 覆盖套话词表/长段阈值/对话比例门禁等 | 使用默认规则 |

## 📋 使用流程

1. 侧边栏选择小说项目（目录自动创建记忆库）
2. 在「🤖 大模型配置」填写 API Key / Base URL / 模型名称，可点「🔌 测试连接」验证，再「💾 保存到.env」
3. 设定章节号、目标字数（默认 7500 字），可选填写自定义剧情要求
4. 点击「🚀 一键躺平生成」跑单章，或设定起止章「🚀 批量连跑」
5. 生成中可点「⏹ 停止生成」协作式取消；完成后在「✍️ 章节生成」回显/下载，在「📖 生成历史」查看正文与运行报告

## 🎛️ 环境变量说明

环境变量共 14 个：13 项收口在 `openmars_core/config.py`（`AppConfig`），`LLM_EXTRACT_MODEL_NAME` 由 `cyber_printer_ultimate.py` 直接读取；`.env.example` 与之一一对应，无多余项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_API_KEY` | 无 | **必填**，大模型 API Key |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | 接口地址（绝大多数服务商以 `/v1` 结尾；`.env.example` 模板默认智谱） |
| `LLM_MODEL_NAME` | `deepseek-chat` | 模型名称（如 `glm-4-flash`、`deepseek-chat`） |
| `LLM_TEMPERATURE` | `0.7` | 温度系数，网文创作推荐 0.7-0.8 |
| `LLM_MAX_TOKENS` | `0` | 单轮最大输出 Token，0 表示不传、由服务商按模型上限决定 |
| `LLM_TIMEOUT` | `0` | 调用超时秒数，0 表示使用 OpenAI SDK 默认超时 |
| `MAX_RETRY` | `3` | 单次调用最大重试次数 |
| `LLM_EXTRACT_MODEL_NAME` | 空 | 写后记忆抽取/一致性审校使用的便宜模型；留空则用主模型 |
| `MAX_CHAPTER_WORDS` | `4500` | 番茄建议区间上限：作 `platform_hint` 边界，并在目标字数未提供时作字数门禁兜底基准 |
| `TOMATO_AUDIT_COMPATIBLE` | `true` | 质量门禁总开关：`true`=不达标自动定向重写（≤2轮）；`false`=只出报告不强制 |
| `CONTENT_SAFETY_CHECK` | `true` | 一致性审校开关：`false` 时零额外 LLM 调用 |
| `LLM_PRICE_IN_PER_1M` | `0` | 输入单价（元/百万Token），配置后运行报告/面板附估算成本 |
| `LLM_PRICE_OUT_PER_1M` | `0` | 输出单价（元/百万Token） |
| `WEBHOOK_URL` | 空 | 移动端告警 Webhook（见下节） |

> 免费模型方案：智谱 GLM-4-Flash 等免费模型可用（是否有限额以服务商政策为准）。使用免费模型时系统自动启用严格串行保护（全局锁 + 前后延迟，检测到并发生成直接拒绝新任务），请勿同时运行多个生成任务。

## 📱 移动端告警配置

在 `.env` 中配置 `WEBHOOK_URL`，按 URL 关键字自动分流（`cyber_printer_ultimate.py:64` `send_mobile_alert`）：

```env
# Bark iOS（GET 请求）
WEBHOOK_URL=https://api.day.app/你的BarkToken

# Server酱（POST 表单，命中 ftqq/sct 关键字）
# WEBHOOK_URL=https://sctapi.ftqq.com/你的SendKey.send

# 飞书/钉钉自定义机器人（POST JSON）
# WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的HookKey
```

未配置时告警只进运行报告留痕，不影响生成。

## 📂 项目结构

```
OpenMars/
├── openmars_core/              # 核心模块
│   ├── config.py               # 环境变量配置收口（14 个环境变量，13 项收口在 AppConfig）
│   ├── query_engine.py         # 同步调用引擎（大纲→排雷→正文三步串行流水线）
│   ├── prompts.py              # 全部提示词模板具名收口（大纲/排雷/正文/抽取/摘要/门禁重写/审校）
│   ├── validators.py           # 零 token 质量门禁 + 可选一致性审校
│   ├── memory_palace.py        # SQLite 记忆宫殿 Schema v2（伏笔台账/角色状态/滚动摘要/JSON迁移）
│   ├── llm_ledger.py           # LLM 调用记账（logs/cost.db）
│   └── logger.py               # loguru 日志（敏感信息脱敏，logs/app_*.log、error_*.log）
├── cyber_printer_ultimate.py   # 主控调度：质量门禁闭环、写后回写、批量连跑、告警、运行报告
├── openmars_panel.py           # Streamlit 面板（4工作区 + 侧边栏配置/批量/用量）
├── tests/                      # 单元测试（unittest，49 项）
├── novel_settings/             # 小说设定（用户创建）
│   ├── 模板/                   # 00/01/02 设定文件模板
│   └── 默认小说/               # 默认小说（含 03-番茄审核铁则.md）
├── output/                     # 输出目录（自动创建）
│   └── <书名>/第N章_时间戳.md + 第N章_报告.json
├── logs/                       # 运行日志 + cost.db 记账库（自动创建）
├── .streamlit/config.toml      # Streamlit 主题与服务器配置
├── run_openmars.sh             # macOS/Linux 一键启动脚本
├── Dockerfile / docker-compose.yml
├── .env.example                # 环境变量模板
└── requirements.txt            # 依赖清单（openai/streamlit/loguru/requests 等）
```

## ✅ 上手自检

| 检查点 | 预期 |
|---|---|
| 浏览器标签页标题 | 「OpenMars 工业级网文创作系统」（`openmars_panel.py:247`） |
| 面板工作区 | 4 个 Tab：章节生成 / 记忆宫殿 / 生成历史 / 帮助说明 |
| 生成一章后 | `output/<书>/第N章_*.md` 与 `第N章_报告.json` 生成；`novel_settings/<书>/memory.db` 更新 |
| 记忆宫殿 Tab | 可见全局摘要、伏笔台账、生成上下文骨架预览 |
| 旧 JSON 迁移 | 旧 `03-动态剧情记忆.json` 数据入库后原文件改名为 `.bak.migrated` |
| 单元测试 | `python -m unittest discover -s tests` 全部通过（当前 49 项） |

## 🔒 安全规范

- 绝不把 `.env` 提交到 Git（已在 `.gitignore` 中）
- 日志自动脱敏 API Key / Token / 密码（`openmars_core/logger.py` `sanitize_message`）
- API Key 只授予最小必要权限，定期轮换

## 🧪 运行测试

```bash
python -m unittest discover -s tests -v
```

覆盖：质量门禁四类检查与规则覆盖、门禁/审校/运行报告接线、批量断点续跑、三步流水线模板渲染与解析、场景计划容错、防复读修剪等。

## 📄 许可证

MIT License，仅供学习研究使用。

## 🙏 致谢

- [Streamlit](https://streamlit.io/)
- [OpenAI Python SDK](https://github.com/openai/openai-python)（OpenAI 兼容接口）
- [loguru](https://github.com/Delgan/loguru)

---

**OpenMars V3.0 祝您创作愉快！** 🎉
