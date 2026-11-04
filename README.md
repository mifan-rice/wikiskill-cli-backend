# wikiskill CLI backend（非官方）

把 [WikiSkill](https://github.com/ashutoshsinghpr7/wikiskill)（论文
arXiv:2608.27454）的共演化循环，接到**任何能一次性跑一个 prompt 的 agent CLI**
上，替代上游内置的 Hermes backend。

上游的核心算法（wiki 共演化、`R_val > R_best` 严格择优、reject 时回滚）不需要
也不应该改；本仓库只补一个「执行层」：在一个隔离 profile 里跑一次 rollout，
把轨迹导成上游 gating 能读的格式。**不含任何具体平台的内部代码、路径或凭据。**

## 它到底做了什么

| 协议方法 | 作用 | 为什么必须 |
|---|---|---|
| `bootstrap_profile` | 建隔离目录（`state/` + `skills/`） | 每次 rollout 不共享记忆/历史，否则 gating 是假阳性 |
| `set_active_skills` | 把 `skills/active(+framework)` **复制**进 profile | 被测 agent 看到的技能集必须**恰好**是候选集 S_k |
| `run` | 写 prompt 文件 → 跑子进程 → 收 stdout/退出码 | 执行层唯一真正干活的方法 |
| `export_session` | 归一化轨迹 JSONL | 上游 gating 读首行 `{tool_call_count, message_count}` |
| `env` / `patch_model` | 注入隔离 home、让模型选择生效 | 否则 CLI 会读用户全局配置，隔离失效 |
| `profile_dir` | 返回 profile 路径 | 上游用于清理/审计 |

两处非显然但必要的设计：

1. **推理轮把 active 技能全文拼进 prompt 前缀**。实测模型不会主动「查技能」，
   只靠工具自取＝注入失效（上游 Hermes 用 `-s` 预加载解决同一问题）。
   framework 轮（maintainer/proposer）**不注入**——它们该自己从 wiki/trace 取材。
2. **交付物检查 + 补交轮**（可选）。真实模型常把内容写在文本回复里就
   `completed`，文件没落盘 → 提案缺失 → 进化链断。配置里声明「这一轮必须产出
   什么文件」，缺失就带着更强的提示重跑，最多 N 次，超了就明确失败。

## 快速开始

```bash
cp config.example.json config.json     # 改 command 成你自己的 CLI
python tests/smoke.py                  # 用内置假 CLI 验证协议（不连真实模型）
```

```python
from backend import CliBackend

BACKEND = CliBackend("config.json")
BACKEND.bootstrap_profile("/path/to/evo-workspace")

# 推理轮：注入当前候选技能
r = BACKEND.run(ws, prompt, tag="iter-1-inference", max_turns=15)
print(r.exit_code, r.session_file)

# 上游 runner 注入形态（harness.evolve(runner=...)）
runner = lambda **kw: BACKEND.run(**kw).as_dict()
```

## config.json 字段

| 字段 | 说明 |
|---|---|
| `command` | **必填**。`list[str]`，支持占位符：`{prompt_file}`（prompt 全文的落盘路径）`{prompt}`（prompt 全文，走 argv）`{workdir}` `{run_dir}` `{tag}` `{model}` `{max_turns}` `{budget}` `{ws}` `{profile}` `{state}` `{skills}` |
| `profile_dir` | profile 目录名，默认 `.wikiskill-cli-home` |
| `env` | 传给子进程的环境变量（可含占位符）。**隔离靠这里**：把 CLI 的 home 指到 `{profile}` |
| `model` / `max_turns` | 占位符默认值（调用方显式传参优先） |
| `session_export` | `{"mode": "cmd", "cmd": [...], "output": "{run_dir}/session.raw.jsonl"}`；`cmd` 留空表示轨迹已由 CLI 自己写到 `output` |
| `deliverables` | `[{"tag_prefix": "maintain", "glob": "wiki/patterns/*.md", "mode": "new_or_changed"}]`；`mode` 也可 `exists`（glob 里可用 `{iter}`） |
| `max_deliverable_retries` | 补交轮上限，默认 0（不检查） |
| `retry_prompt` | 补交提示模板，可含 `{missing}` `{tag}` |
| `timeout_grace` | 在 `run_budget` 之上额外等待的秒数 |

环境变量 `WIKISKILL_CLI_CONFIG` 可代替传参。

## 已知边界（请当真）

* **`session_export.mode="stdout"` 是降级方案**：只用 stdout 合成一条消息，
  首行写 `{"tool_call_count": null, "synthetic": true}`。它**不是**真实工具轨迹，
  上游按「有没有调工具」做启发式判断时会失真——要么接 `cmd` 模式导出真轨迹，
  要么接受 gating 质量下降。
* **隔离强度取决于 CLI**：本仓库只负责给独立 `state/`、`skills/` 和隔离 home；
  如果 CLI 还会读写用户全局配置、全局记忆，那部分隔离不在本层能力范围内。
* **非零退出不重试**：CLI 自己失败就如实把退出码和 stderr 交上去，不吞。
* 未做并发 rollout（同一 workspace 并发会让 `set_active_skills` 互相踩）。

## 接真实 CLI

### `examples/hermes.json`（Hermes Agent v0.21.5，**本机真跑通过**）

`-z PROMPT` 是 oneshot 模式：只把最终回复打到 stdout（无 banner、无 session_id 行），
审批自动 bypass，同时会加载 CWD 的 tools / memory / rules / AGENTS.md。
它只吃 argv 形式的 prompt，所以这里用 `{prompt}`（全文进 argv）而不是 `{prompt_file}`——
**argv 有长度上限**（Windows ≈32767 字符），prompt 很大时请换成 stdin 型 CLI。
`--usage-file` 在跑完后写用量 JSON（含估算成本 / token / api_calls，跑失败也写），核账看它。

本机实测（推理轮，prompt 为 `Reply with exactly: PONG`）：exit 0、33.3s、
stdout = `PONG`、`usage.json` 记 deepseek-flash / in 49562 / out 3 / 1 次 API / $0.0074。

⚠️ 那 49562 输入 tokens 是 **CLI 自带的系统提示与工具清单**，不是任务本身：
套一层 agent CLI 的基线开销比直接调 API 高两个量级，做预算要按「每次调用」算。

### `examples/codex.json`（参数来自官方文档，**未本机实测**）

关键点：

* prompt 走 **stdin**（`codex exec ... -`），所以 `command` 里不出现 `{prompt_file}`，
  配置项 `"stdin": "prompt_file"` 会让 backend 把 prompt 全文灌进子进程 stdin；
* `{workdir}` 传给 `--cd`，agent 在 workspace 里改写文件；
* `env.CODEX_HOME = {profile}` 把 codex 自己的家目录也钉进独立 profile。

先验组装、再花钱跑模型，两步分开：

```bash
# ① 只到 exec 边界：技能注入、占位符展开、stdin 策略（不出网、不花钱）
python examples/codex_dryrun.py
# ② 真跑（需要该 exe 可执行 + 可出网到模型 API）
python -c "import json,backend;backend.CliBackend(json.load(open('examples/codex.json'))).run('.', '你的任务', tag='iter-1-inference')"
```

⚠️ `codex.json` 的参数来自官方文档，**没有在本机跑通过**（本机 codex 额度已耗尽）。
执行层白名单已把 `hermes` / `codex` 加入 `ALLOWED_COMMANDS`（**不并入 `SAFE_SHELL`**，
每次调用仍走审批与网络闸门），但「改了代码 ≠ 运行中生效」：要重启后端才落到进程里，
重启前只能走 `subprocess` 过渡通道（上面 hermes 实测就是这么跑的）。
第一次真跑 codex 前务必 `codex exec --help` 对一遍参数名，并把 `model` 改成账号可用的名字。

## 持续集成

`.github/workflows/ci.yml`（已随仓库提供）在 **Linux + Windows × Python 3.9 / 3.12**
上跑三件事：字节码编译、`tests/smoke.py`（假 CLI，无模型无网络）、`ruff check`
（只挑 `E9`/`F`：语法错与未定义名）。本地等价命令：

```bash
python -m compileall -q backend.py fixtures tests
python tests/smoke.py
ruff check .
```

注：lint 步骤标了 `continue-on-error`（**提示性、不拦合并**）——作者本地无网络
装不上 ruff，未能自证首次跑绿；编译 + smoke 两步才是硬闸。

## 许可


MIT（见 `LICENSE`）。基于论文与上游公开协议的**独立实现**，不含上游代码；
上游仓库未声明许可证，故此处不复制其任何文件。署名信息见 `NOTICE`。
