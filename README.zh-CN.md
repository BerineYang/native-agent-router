# Native Agent Runtime Router（NAR）

[English](README.md) | 简体中文

**通过一个小型、确定性、无模型的内核，将编程任务从编排 Agent（Codex / opencode / Claude Code）路由给原生 CLI worker Agent（ZCode 或任何兼容 ACP 的 Agent）。**

内核本身从不调用模型。它负责管理任务状态、有界等待、工作区锁、固定验证、一次预授权修复、带确认的取消、预算、恢复、用量统计和各 Agent 评分，让编排 Agent 把 token 用在判断上，而不是轮询和整理日志。

```
编排 Agent（Codex / opencode / Claude Code）
        |  MCP（5 个稳定工具）或 CLI（`nar`）
        v
  无模型内核：任务、锁、验证、修复、预算、恢复
        v
  可插拔原生适配器
   ├── zcode-native   （ZCode app-server 协议 0.16，已在 0.16.5 上实机验证）
   ├── acp-generic    （Agent Client Protocol，已在 opencode 1.18.15 上实机验证）
   └── 你的适配器     （只需一个小类，参见 docs/BACKENDS.md）
```

**其他原生 Agent 正在支持中……**

- MCP：`nar-mcp` 恰好暴露五个工具：`agents`、`run`、`wait`、`inspect`、`cancel`。新增后端通过**配置**完成，不增加新工具。
- CLI：`nar` 与 MCP 共用同一个内核，适合调试、脚本和人工操作。
- Token 管理：默认返回紧凑结果；完整日志和 diff 保存在磁盘上，通过 `inspect` 分页读取；每项任务的确定性评分不消耗 token，可用于后续路由。

## 安装

需要 Python 3.10 或更高版本。ZCode 适配器还需要 Node.js 22 或更高版本，以及 ZCode 桌面应用（或位于 `PATH` 中的 CLI）。ACP Agent 需要对应的 ACP server 可执行文件。

请从官方 GitHub 仓库获取并安装源码：

```bash
git clone https://github.com/BerineYang/native-agent-router.git
cd native-agent-router
python -m pip install .
```

如果 `git clone` 不稳定，请直接下载官方 GitHub 仓库的
[`v1.0.0` 分支源码 ZIP](https://github.com/BerineYang/native-agent-router/archive/refs/heads/v1.0.0.zip)，
解压后在该目录打开终端并运行：

```bash
python -m pip install .
```

如果需要参与开发，将安装命令换成 `python -m pip install -e .`。在仓库目录中也可以运行 `pipx install .`。

Windows 用户应优先使用模块形式检查安装。即使 Python 的脚本目录没有加入 `PATH`，该命令也能正常运行：

```bash
python -m native_agent_router doctor
```

如果脚本目录已经位于 `PATH`，也可以使用等效的短命令：

```bash
nar doctor
```

自检会使用 `[OK]`、`[X]` 和 `[i]` 状态标记显示 ZCode bundle、Node.js、Git 和配置检查结果，并在缺少组件时给出处理方法。它不会输出凭据。只有准备使用 `zcode-native` 适配器时，缺少 ZCode 才需要处理。

## 快速开始

1. 创建配置（可选；没有配置时也支持自动发现）：

```bash
mkdir -p ~/.native-agent-router
cp config.example.json ~/.native-agent-router/agents.json
```

2. 列出 Agent，提交一个完整工作单元，等待并检查结果：

```bash
nar agents
nar run zcode "按照文档字符串实现 flags.py 中的 parse_flag()；只修改 flags.py" \
    /abs/path/to/project --mode build --policy allow --scope flags.py --verify "python -m pytest -q" --wait 600
nar wait <task_id>
nar inspect <task_id> summary
nar inspect <task_id> diagnose      # 已清理敏感信息、可分享的错误报告
```

3. 同一模块的后续工作继续使用**同一个原生会话**：

```bash
nar run zcode "现在处理之前讨论的 --verbose 边界情况" /abs/path/to/project \
    --session-ref <previous_task_id> --mode build --policy allow --verify "python -m pytest -q"
```

## 配置

配置文件查找顺序：`--config` 参数 → `NAR_CONFIG` 环境变量 → `<home>/agents.json`（`home` 来自 `NAR_HOME`，未设置时为 `~/.native-agent-router`）→ 内置默认值与自动发现。

### 顶层设置

| 键 | 默认值 | 含义 |
|---|---:|---|
| `wait_default_sec` | 120 | `wait` 的默认有界等待时间 |
| `wait_max_sec` | 3600 | 单次 `wait` 的上限，不限制任务自身的超时时间 |
| `timeout_default_sec` | 1800 | 每轮任务的默认超时时间 |
| `verify_timeout_sec` | 600 | 每条验证命令的超时时间 |
| `max_result_chars` | 12000 | 返回给编排 Agent 的结果摘要字符预算 |
| `repair_default` | true | 每项任务允许一次预授权的定向修复 |
| `verify_allow_shell` | false | 验证命令默认不经 shell 执行，而是使用 argv / `shlex.split`；只有确实需要 shell 语义时才设为 true |
| `require_git_baseline` | false | 失败关闭模式：拒绝在非 Git 工作区启动 worker，确保修改始终可审计 |

### Agent

每项任务都必须明确指定 `agent_id`。系统不存在可被其他调用静默修改的全局“当前后端”。

| 键 | 适用范围 | 含义 |
|---|---|---|
| `adapter` | 全部 | `zcode-native`、`acp-generic`，或测试用的 `stub` |
| `enabled` | 全部 | 设为 false 时隐藏该 Agent |
| `command` | acp-generic | 启动 ACP server 的 argv，例如 `["opencode","acp"]` |
| `cwd_arg` | acp-generic | 某些 ACP server 需要 `--cwd <ws>`，在此填写参数名 |
| `model` | 两者 | 模型选择，参见下文 |
| `model_args` | acp-generic | 通过 CLI 参数注入模型的模板，例如 `["--model","{model}"]` |
| `provider` | zcode-native | 来自你自己的 `~/.zcode/v2/config.json` 的 ZCode provider ID |
| `thought_level` | zcode-native | 例如 `low`、`high`、`max`，取决于模型能力 |
| `default_mode` | zcode-native | `plan`（只读）、`build`、`edit` 或 `yolo` |
| `default_permission_policy` | 两者 | `deny`（诚实的默认值）或 `allow`（在单项任务内自动批准） |
| `tool_allowlist` | zcode-native | 原生工具集限制，例如 `["Read","Grep","Glob"]` |
| `zcode_home` | zcode-native | `"isolated"` 表示将 NAR 的 ZCode 会话保存在独立 home 中，参见 `docs/SECURITY.md` |
| `credentials` | zcode-native | 默认 `auto`：复用用户自己的 ZCode 配置，参见下文 |
| `env` | 两者 | Agent 子进程的额外环境变量；不要把秘密写在这里，因为它们会进入配置文件 |

### 模型选择

- **ZCode（`zcode-native`）**：设置 `provider`、`model`，并可选设置 `thought_level`。NAR 从你自己的 `~/.zcode/v2/config.json` 生成 ZCode 0.16 的 `runtimeModel` payload，只在协议中传递。默认不会把任何凭据复制到磁盘，不会改变 provider、base URL 或计费通道，并支持冷启动 `session/resume`（已在 0.16.5 上实机验证）。
- **ACP Agent（`acp-generic`）**：NAR 会先尝试 ACP 原生路径 `session/set_config_option` / `session/set_model`。该方式适用于 opencode，其 `session/new` 会公布 `model` 配置项。如果不可用，再回退到 `model_args` CLI 参数替换，例如 `gemini --experimental-acp --model X`。

### ZCode 自动发现（如何找到 `zcode.cjs`）

查找顺序：`ZCODE_BIN` 环境变量 → `PATH` 中的 `zcode` → Windows 注册表卸载项中的 `InstallLocation` → `%LOCALAPPDATA%\Programs\ZCode` → 各磁盘的 `Program Files\ZCode`。如果都没有匹配，`nar doctor` 会明确告诉你需要设置什么。桌面应用不把 CLI 加入 `PATH` 是正常情况。

### 关于凭据

`credentials: "auto"` 会以只读方式读取你已经在 ZCode 桌面应用中配置的 provider，并在协议 payload 中把同一 provider、模型、base URL 和 key 传给无界面的 app-server。NAR 不记录 key、不把 key 上传到任何地方、不切换计费通道，也不绕过认证或套餐限制。如果希望使用独立会话存储，可设置 `zcode_home: "isolated"`；相关权衡见 `docs/SECURITY.md`。

## MCP 集成

通过 stdio 运行 `nar-mcp`。

**Codex**（`~/.codex/config.toml`）：

```toml
[mcp_servers.native-agent-router]
command = "nar-mcp"
args = []
```

**opencode**（项目中的 `opencode.json` 或 `~/.config/opencode/`）：

```json
{ "mcp": { "native-agent-router": { "type": "local", "command": ["nar-mcp"], "enabled": true } } }
```

**Claude Code**（`.mcp.json`）：

```json
{ "mcpServers": { "native-agent-router": { "command": "nar-mcp" } } }
```

然后用一句话告诉编排 Agent：

> “先运行 `agents`，再使用 `run` 把这项任务连同 scope 和 verify 交给 `zcode`，并用 `wait` 等待结果。”

可将 `skill/native-agent-router/SKILL.md` 中的委派规则加入任何 Agent 的技能目录。

## 五个 MCP 工具

| 工具 | 用途 |
|---|---|
| `agents` | 返回已配置的 Agent、能力和评分统计，供路由决策使用 |
| `run` | 提交完整工作单元，包括 goal、scope、forbid、verify、budget、mode 和 session_ref |
| `wait` | 有界等待任务进入终态或 blocked，不进行忙轮询 |
| `inspect` | 按需分页读取 status、summary、diff、verify、log、raw、usage 或 `diagnose` |
| `cancel` | 请求停止，并报告是否已经确认终止 |

`run` 返回只代表任务已提交，不代表任务完成。只有 `succeeded`、`failed`、`cancelled`、`blocked`、`interrupted` 是最终状态。任务进入 `blocked` 后，内核会保留工作区锁并继续观察原生会话，直到出现真正的终止事件，或者你执行 `cancel` / `kill`。

## CLI 参考

```
nar agents | doctor | list
nar run <agent_id> <goal> <workspace> [--scope ...] [--verify "cmd"] [--mode build]
        [--policy allow|deny] [--session-ref TASK] [--timeout N] [--wait N]
        [--budget N] [--idempotency-key K] [--no-repair]
nar wait <task_id> [--timeout N]
nar inspect <task_id> [status|summary|diff|verify|log|raw|usage|diagnose] [--offset N] [--limit N]
nar cancel <task_id>
nar kill <task_id> --yes        # 强制终止 Agent 并释放锁，属于用户边界
nar stats [agent_id]            # 确定性的各 Agent 评分统计
```

## 测试

```bash
pip install -e ".[dev]"
pytest -q                 # 66 项测试，不调用模型、不访问网络
pytest -q -m real         # 可选：调用本机真实 Agent，会消耗 token
```

## 文档

[架构](docs/ARCHITECTURE.md) ·
[后端](docs/BACKENDS.md) ·
[验证记录](docs/VERIFY.md) ·
[基准测试](docs/BENCHMARK.md) ·
[安全](docs/SECURITY.md) ·
[回滚](docs/ROLLBACK.md) ·
[技能](skill/native-agent-router/SKILL.md)

## 当前状态与边界

- 已在本机实机验证：ZCode 0.16.5，包括只读、原生续接、编辑与验证、权限处理和真实用量；opencode 1.18.15，包括 ACP 握手、模型切换和会话续接。证据位于 `docs/VERIFY.md`。
- 尚未在本机验证：Gemini CLI（未安装）、作为 ACP 使用的 Claude Code（已安装，但据用户反馈当前不可用），以及作为编排客户端的 Codex。
- 项目不宣称“最优”，也不承诺固定的节省比例；已测量和未测量的内容见 `docs/BENCHMARK.md`。
- 其他原生 Agent 正在支持中……
- 本项目采用 MIT 许可证，是独立社区项目，与 Z.AI/ZCode、OpenAI Codex 或 SST/opencode 无附属关系。
