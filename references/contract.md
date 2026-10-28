# computer-use skill 接口契约 v1

冻结日期：2026-10-25。部署修订日期：2026-10-27（系统 Python 与按需自动启动，见 §1）。底层驱动固定为 Cua Driver 0.38.x，standard 模式。

契约分两层，都算冻结：

- **A 层（agent ↔ wrapper）**：CLI 参数、stdout 信封、exit code、产物路径。
- **B 层（wrapper ↔ 本地模型）**：请求/响应形状。模型本身可插拔，形状不可变。

A/B 层接口改动升 `contract` 版本号；2026-10-27 用户指定的部署修订见 §1，接口形状未变，保持 contract 1。wrapper 启动时把 `contract` 写进输出，agent 校验主版本不一致就拒绝。

---

## 1. 入口

```
python ~/.claude/skills/computer-use/scripts/run.py <intent> --json
python ~/.claude/skills/computer-use/scripts/health.py [--json]
python ~/.claude/skills/computer-use/scripts/stop.py [--json]
```

部署修订（2026-10-27，依据用户明确要求）：三个脚本使用计算机环境中已有的 Python 3.12+，依赖为 requirements.txt 声明的 Pillow；skill 不附带 Python 或 venv。`run.py`、`health.py` 初始检查复用兼容 standard daemon，未运行时自动后台启动补丁版 driver，最多等 15 秒并计入总超时。已有不兼容 daemon 不自动重配，会话建立后不自动重启，`stop.py` 和清理从不启动 daemon。此段替代原始设计中的固定 venv 解释器部署要求；A/B 层 CLI、JSON、退出码和模型形状保持 contract 1，原始设计文件仍作为修订前记录保留。

### run.py 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `<intent>` | 必填 | 自然语言意图，原样透传给模型，wrapper 不解析。超过 4000 字符截断 |
| `--json` | 恒开 | 兼容别名，不改变行为。stdout 永远是单个 JSON 对象 |
| `--timeout` | 120 | 秒。上限 600，超过按 600 处理 |
| `--budget` | 30 | 模型决策轮数上限。上限 100 |
| `--background-only` | 关 | 禁止任何前台投递，退回纯后台模式 |
| `--app` | 无 | 目标应用可执行名，可重复。用于收窄观察范围，不给就由模型自己找 |
| `--keep-artifacts` | 关 | 保留本次运行目录（截图、transcript）供事后排查。默认全部删除 |
| `--artifacts-dir` | 见 §5 | 配合 `--keep-artifacts` 指定目录。单独给会自动开启保留 |
| `--no-overlay` | 关 | 不显示"AI 操控中"覆盖层。**同时禁用前台动作** |

没有 `--allow-foreground`：焦点让渡已默认允许，条件是覆盖层 + ESC 钩子必须就位（§6.3）。要禁止就用 `--background-only`。

### 退出码

| code | status | 含义 |
|---|---|---|
| 0 | `done` | 意图达成且自检通过 |
| 1 | `error` | wrapper 或依赖出错（驱动/模型/解释器），非业务结果 |
| 2 | `blocked` | 环境层拒绝，重试一次后可放弃 |
| 3 | `needs_human` | 必须由人决定或动手，agent 不得自行继续 |
| 124 | `timeout` | 超时或超预算 |

124 是 `timeout` 专用；预算耗尽也用 124（`reason: budget_exhausted`），不另开码。

退出码可以新增（5–9 留给后续类别），但新增必须同时更新 REASON 表和 SKILL.md 的分支说明。判断"错在哪"的职责落在信封里的 `error` / `reason` / `last_step` / `last_observation`，退出码只做粗分类，所以宁可加字段也不靠堆码。

---

## 2. stdout 信封

stdout 有且只有一个 JSON 对象，无其他输出。日志、进度、模型推理片段全部走 stderr。

```json
{
  "contract": 1,
  "ok": false,
  "status": "blocked",
  "reason": "background_unavailable",
  "message": "Edge 窗口拒绝后台输入，当前为后台模式",
  "session_id": "cu-20261025T041500-a1b2c3",
  "intent": "在 Edge 里把这个页面的标题复制出来",
  "result": null,
  "error": {
    "code": "background_unavailable",
    "detail": "type_text 投递到 pid 25872 (Chrome_WidgetWin_1) 被丢弃",
    "raw": "{\"code\": \"background_unavailable\", \"target_class\": \"Chrome_WidgetWin_1\", ...}",
    "retryable": true,
    "hint": "用 --background-only 之外的方式重跑，或改用 browser_* 工具族"
  },
  "artifacts": {
    "kept": false,
    "dir": null,
    "last_screenshot": null
  },
  "last_step": {
    "index": 4,
    "tool": "type_text",
    "target": {"element_token": "s00000010:7"},
    "error": {"code": "background_unavailable", "target_class": "Chrome_WidgetWin_1"}
  },
  "last_observation": {
    "app": "edge.exe",
    "window_title": "示例页面 - Microsoft Edge",
    "elements_excerpt": ["[1] Pane \"\"", "[7] Button \"关闭\""]
  },
  "steps_used": 4,
  "elapsed_sec": 18.4,
  "model": {"adapter": "openai-compatible", "name": "local", "observation": "vision"},
  "warnings": []
}
```

字段规则：

- `ok` 恒等于 `status == "done"`。
- `reason` 只在 `ok: false` 时非 null，取值见 §3。
- `error` 只在 `status` 为 `blocked` / `error` / `timeout` 时非 null，`done` 和 `needs_human` 为 null（用户中止不是程序的错）。
  - `error.detail`：一句话说清失败在哪个动作、作用在哪个对象上。
  - `error.raw`：驱动或模型返回的原始错误串，原样保留，不裁剪。
  - `error.retryable`：同一意图重跑是否可能成功。agent 据此决定要不要重试一次。
  - `error.hint`：给 agent 的下一步建议，一句话，可以说"换用 XXX 工具族"或"需要人确认"。
- `result` 只在 `ok: true` 时非 null，形状见 §2.1。
- 默认退出前删除本次产生的全部中间文件，`artifacts.kept` 为 `false`，`dir` / `last_screenshot` 为 `null`。只有 `--keep-artifacts` 时才有路径。
- 因此 `last_observation` 必须自带文本摘要（元素树片段，最多 20 行），失败时 agent 靠它判断现场，不依赖任何文件。
- 中止和超时也照删。这是对早前"超时返回截图路径"的覆盖：文件和返回值只能保一个，选了不留文件。
- `steps_used` 是模型决策轮数，不是底层动作数。
- `warnings` 收集不影响结果的异常，例如 `effect: "unverifiable"`、前台升级、覆盖层不可见。
- 字段只增不减；新增可选字段不升 `contract`。

### 2.1 result

```json
{
  "summary": "已把文档首个标题改为一级标题",
  "evidence": {
    "kind": "readback",
    "detail": "元素 s00000010:2 的 value 现在以「金融交易备忘录」开头",
    "element_token": "s00000010:2"
  },
  "apps": ["notepad.exe"],
  "windows": [{"title": "financial-memo.md - Notepad", "window_id": 12518746}],
  "steps_taken": 7,
  "foreground_used": false
}
```

`evidence.kind` 限定 `readback` / `screenshot` / `state_change` / `dialog_gone`。没有 evidence 的 `done` 不接受（§6.6）。

---

## 3. reason 取值表

冻结枚举，新增算 minor 变更。

| reason | 典型 status | 含义 |
|---|---|---|
| `background_unavailable` | blocked | 目标窗口明确拒绝后台输入，且当前不允许前台 |
| `foreground_not_allowed` | blocked | 需要前台，但被 `--background-only` / `--no-overlay` 挡住，或覆盖层起不来 |
| `element_stale` | blocked | `element_token` 失效，重试一次仍失效 |
| `target_not_found` | blocked | 找不到窗口或应用 |
| `ambiguous_target` | blocked | 多个候选无法消歧 |
| `app_not_allowed` | blocked | 目标命中策略 deny 列表 |
| `verification_failed` | blocked | 模型声称完成，独立校验不通过，且重试一次仍不通过 |
| `budget_exhausted` | timeout | 轮数或时间用完 |
| `user_escape` | needs_human | 用户按 ESC |
| `user_cancel` | needs_human | 经 `stop.py` 中止 |
| `escalated_after_blocked` | needs_human | 连续两次 blocked，按规则升级 |
| `needs_auth` | needs_human | 登录、验证码、二次验证 |
| `needs_decision` | needs_human | 不可逆或有歧义的操作，需要人拍板 |
| `driver_unavailable` | error | 驱动没起或起不来 |
| `model_unavailable` | error | 模型端点不通或返回不可解析 |
| `internal_error` | error | 其他 |

每个 `reason` 在代码里绑一个默认 `error.hint`，形如"改前台重跑"、"换 browser_* 工具族"、"需要人登录"、"把窗口切到前台后重试"。hint 可以覆盖，但不能为空——agent 拿不到方向就等于没拿到信息。


---

## 4. 语义边界

- wrapper **绝不读 stdin**，不做交互式确认。所有需要人类介入的情况一律以 `needs_human` 退出，由 agent 转述给用户。
- 一次 `run.py` = 一个意图 = 一个 `session_id`。跑完即退，无 REPL、无后台常驻服务；覆盖层进程随 run 生死。
- 跨 run 无记忆。不缓存上次的窗口、元素或结论。
- 同一个动作最多自动重试一次。连续两次 `blocked` 立即转 `needs_human`（`escalated_after_blocked`），不再尝试。
- 意图是命令式描述要达成的状态，不是步骤清单。模型负责规划，agent 不介入中间决策。

---

## 5. 产物与清理

运行期在系统临时目录建一个本次专用的目录：

```
%LOCALAPPDATA%\Temp\claude-cu\runs\<session_id>\
  runtime.json      # pid, started_at, phase, stop_requested
  transcript.jsonl  # 每轮一行：观察摘要、模型决策、执行结果
  step_NN.png       # 每轮一张窗口级截图
  result.json       # 最终信封的副本
  stop.flag         # 被停止时出现
```

进程退出前，除 stdout 的信封外**全部删除，不留任何文件**：截图、transcript、runtime、stop.flag、目录本身。`try/finally` 保证正常、超时、ESC、异常四种路径都会清。删不掉的（文件被占用）记进 `warnings`，并把目录留给下次清扫。

兜底清扫：每次 `run.py` 启动时先删 `/tmp/claude-cu/runs/` 下超过 1 小时的所有目录，覆盖被强杀或断电留下的残骸。`stop.py` 收尾时也执行一次。

`--keep-artifacts` 是唯一的保留入口，给人和调试用；此时 `artifacts.kept` 为 `true` 并给出路径，SKILL.md 明确写"只在排查时用"。

产物目录名和位置不属于契约，agent 不得自己拼路径去读文件——需要的信息都在 stdout 里。

---

## 6. 运行时约束

### 6.1 session 标签

每次 `cua-driver call` 都必须带 `"session": "<session_id>"`。这是硬约束，不是优化：

- 不带 → `element_token` 跨进程立即 `stale_element_token`
- 不带 → `kill_app` 报 `foreign_process_termination_denied`
- `session_id` 格式：`cu-<UTC %Y%m%dT%H%M%S>-<6 位 hex>`

### 6.2 观察

- 每轮做全量 `get_window_state`，不依赖 `since` 增量读（0.38.0 该路径返回 `typed_output_mismatch`，不可用）。
- 默认只喂元素树，**不每轮截图**。只有这几种情况才落一张截图：
  - `elements_complete == false` 或 `truncated == true`
  - 过滤后（去掉既无 `value` 又无 `actions` 的装饰节点）剩余元素少于 5 个——Electron 和 Chromium 的典型形态
  - 模型上一轮显式返回 `need_vision: true`
  - 动作失败后的重试轮
- `observation: "text"` 的模型实例永不落截图，上述条件对它无效。
- 每个 run 截图上限 30 张，到顶后只在 `warnings` 里降级记录，不再拍。
- 截图用 `screenshot_out_file` 落盘，不用内嵌 base64（416 KB vs 36 KB + 285 KB）。文件随 run 结束删除。
- 优先窗口级截图；`get_desktop_state` 只在模型明确要求看全屏时使用（1.6 MB）。
- 窗口级截图不包含覆盖层，模型不会看到自己的提示框和光标。

### 6.3 前台与守卫

- 默认允许前台。守卫进程在 run 开始时就启动（`--no-overlay`、`--background-only` 或策略禁止前台时除外），它负责置顶点击穿透的"AI 操控中 · 按 ESC 中止"提示条和 `WH_KEYBOARD_LL` ESC 钩子；观察和模型决策阶段同样显示接管提示。
- 任何前台动作之前复核守卫就绪。守卫起不来 → 记 `overlay_not_shown` 警告并继续只读/后台工作，但不执行前台动作，前台请求返回 `blocked` / `foreground_not_allowed`。
- 覆盖层必须对截图不可见：窗口加 `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)`（Win10 2004+，本机 Win11 支持）。驱动走 PrintWindow / DXGI 都拍不到它。
- 覆盖层必须能盖住全屏应用：`WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW`，配合 DWM 合成。无边框全屏（DXGI flip，现代游戏和浏览器的主流做法）可以正常覆盖。
- 真正的 exclusive fullscreen（旧式独占交换链）会绕过 DWM，任何顶层窗口都盖不上去。检测到目标窗口是全屏独占时，改为在 run 开始时弹一次前置确认（"目标独占全屏，覆盖层不可见，ESC 仍可用"），ESC 钩子照装，不阻断执行。这一点写进 SKILL.md。
- ESC 命中 → 写 `stop.flag`，吞掉该按键，杀掉在途 `cua-driver call` 子进程，返回 `3` / `user_escape`。
- 第一次前台动作之前给 2 秒倒计时提示。
- 后台投递返回 `background_unavailable` 时自动转前台重试，并在 `warnings` 里记一条。

### 6.4 覆盖层视觉规范

覆盖层是一个置顶、点击穿透、对截图不可见的全屏透明窗口。它要同时做到两件事：让用户一眼看出机器正被接管，以及把 AI 的动作用视觉画出来。

**接管标识**

- 屏幕四边一圈 3 px 呼吸描边，2 秒一个周期，品牌蓝。用户在任何界面都能看到边缘在动，这是最强的一眼判断。
- 顶部居中的常驻胶囊：`● AI 操控中 · 按 ESC 中止`，左侧圆点表示运行中。
- 胶囊第二行写当前动作，例如 `点击「保存」· 记事本`、`输入 24 个字符 · Edge`、`滚动 · Excel`。只有"被接管"没有"在干什么"，用户仍然没法判断要不要中止。
- 首次前台接管时胶囊闪一次，先显示 2 秒倒计时再转入常驻。

**AI 光标**

- 一个放大的箭头指针，约系统光标 3.5 倍。白色粗描边 + 浅蓝半透明填充 + 轻微投影，保证在浅色和深色背景上都清楚。
- 箭头尖端就是真实点击点，热点严格对齐尖端，不做偏移。
- 跟随真实光标：轮询 `GetCursorPos`，60 Hz，做约 60 ms 的平滑插值，避免瞬移。
- 坐标动作：先把箭头平滑移到目标点，再执行点击；点击瞬间给一圈 200 ms 的收缩脉冲。
- 元素动作（UIA 或后台投递，没有坐标）：不动箭头，只在胶囊里写清操作的是哪个元素。
- 系统光标保持可见，不隐藏——用户还要自己用机器。若桌面级截图里带上了系统光标，属于可接受的小瑕疵。

**实现约束**

- 分层窗口 + 每像素 alpha（`UpdateLayeredWindow`），不用色键透明，否则抗锯齿边缘会出现彩边。
- 样式 `WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`：点击穿透、不进 Alt-Tab、不抢焦点。
- 箭头和描边同样走 `WDA_EXCLUDEFROMCAPTURE`，不进任何截图。
- 渲染循环、ESC 钩子、`stop.flag` 轮询放在同一个进程里，随 run 生死。
- 目标窗口是真 exclusive fullscreen 时覆盖层画不上去，此时至少保证 ESC 钩子和开跑前提示可用。

### 6.5 动作批处理

- 模型一次给 1~5 个动作，wrapper 用 `run_steps` 打包成一次调用，`observe: {}` 在同一次调用里拿回变化。
- 理由是固定开销：每次 `cua-driver call` 约 0.9–1.1 秒。
- 禁用 `run_script`（任意 JS 太宽）和 `kill_app`（留给 `stop.py`）。

### 6.6 完成校验

模型返回 `done` 后，wrapper 必须独立校验一次：读回目标元素值、或对比截图、或确认对话框已消失。不通过则把校验结果回灌给模型，最多再给一轮；仍不通过 → `blocked` / `verification_failed`。

---

## 7. B 层：模型适配

模型实现可换，下面的形状不可换。

### 7.0 兼容的模型类别

本 skill 不做选型，只声明能接哪几类：

| 类别 | `observation` | 需要的输入 | 典型目标 |
|---|---|---|---|
| 纯文本 | `text` | `elements` 或 `tree_markdown` | 原生 Win32：记事本 61 元素 / 5.9 KB markdown，字符映射表 15 元素 |
| 视觉 | `vision` | 上述之一 + `screenshot_path` | Chromium / Electron：Cursor 仅 3 个元素、Edge 25 个，必须看图 |
| 多模态 | `vision` | 两者都给 | 混合场景，一套通吃 |

`observation` 由模型配置声明。声明 `text` 的实例永不落截图；声明 `vision` 的实例也只在 §6.2 的触发条件下才收图，不是每轮都收。

### 7.1 请求

```json
{
  "contract": 1,
  "intent": "...",
  "step": 3,
  "budget_left": 27,
  "observation": {
    "app": "notepad.exe",
    "window_title": "financial-memo.md - Notepad",
    "window_id": 12518746,
    "elements": [{"element_token": "...", "role": "Document", "label": "文本编辑器", "value": "...", "actions": ["set_value"], "frame": {"x":0,"y":0,"w":0,"h":0}}],
    "tree_markdown": "- Window ...",
    "screenshot_path": "C:\\...\\step_03.png"
  },
  "history": [
    {"step": 1, "tool": "click", "target": "s00000010:2", "result": "ok"},
    {"step": 2, "tool": "type_text", "result": "effect=unverifiable"}
  ],
  "last_error": {"code": "background_unavailable", "target_class": "Chrome_WidgetWin_1"}
}
```

- `elements` 与 `tree_markdown` 二选一喂，取决于模型实现；两者之一必须给。
- 纯文本模型只拿 `elements` / `tree_markdown`，`screenshot_path` 可空。
- 视觉模型额外取 `screenshot_path` 指向的文件。
- `history` 只回放最近 8 轮，避免上下文线性膨胀。

### 7.2 响应

```json
{
  "thought": "标题在第一行，选中后设为一级标题",
  "actions": [
    {"tool": "click", "target": {"element_token": "s00000010:2"}, "args": {}},
    {"tool": "hotkey", "args": {"keys": ["ctrl", "1"]}}
  ],
  "expect": {"tool": "get_window_state", "element_token": "s00000010:2"},
  "need_vision": false,
  "done": null,
  "ask_human": null
}
```

`done` 与 `ask_human` 互斥，且都不能和 `actions` 同时非空。`need_vision: true` 表示元素树不足以判断，下一轮请求会附上截图；连续 3 轮都为 true 则忽略该请求并记 `warnings`，防止无界拉图。

```json
{"done": {"summary": "...", "evidence": {"kind": "readback", "detail": "..."}}}
{"ask_human": {"reason": "needs_decision", "message": "要覆盖已存在的同名文件吗？"}}
```

### 7.3 动作词表

只允许驱动原生的这些：

`click` `double_click` `right_click` `type_text` `press_key` `hotkey` `scroll` `drag` `set_value` `launch_app` `invoke_menu` `verify_state` `get_window_state` `list_windows`

`target` 二选一：`{"element_token": "..."}` 或 `{"x": int, "y": int}`。`x/y` 坐标用屏幕绝对坐标，不用截图坐标。

模型输出不合规（词表外工具、同时给两种 target、动作数 > 5）→ 视为 `model_unavailable`，不猜测意图。

### 7.4 接入协议

配置在 `~/.claude/skills/computer-use/model.json`，两种适配器二选一：

```json
{"adapter": "openai-compatible", "endpoint": "http://127.0.0.1:8080/v1/chat/completions", "model": "xxx", "observation": "vision", "timeout_sec": 60, "max_actions_per_turn": 5}
```

```json
{"adapter": "command", "command": ["C:\\path\\to\\model.exe", "--json"], "observation": "text", "timeout_sec": 60, "max_actions_per_turn": 5}
```

- `openai-compatible`：POST 到 `endpoint`，§7.1 的 JSON 序列化后作为单条 user message，要求返回 §7.2 的 JSON。覆盖 llama.cpp server、vLLM、LM Studio、Ollama `/v1`、以及多数本地推理服务。
- `command`：启动子进程，stdin 写 §7.1，stdout 读 §7.2，进程退出码非 0 即失败。用于不兼容 OpenAI 协议的推理栈。
- 两者都必须**一次性返回完整 JSON，不支持流式**。wrapper 只解析 JSON，不解析自然语言；解析失败即 `model_unavailable`。
- `observation` 取 `text` / `vision`，决定 §7.1 是否填 `screenshot_path`。
- 模型配置缺失时 `run.py` 直接 `error` / `model_unavailable`，不降级到任何内置模型。

---

## 8. 策略文件

`~/.claude/skills/computer-use/policy.json`，缺失时用内置默认值并在 `warnings` 里提示一次。

```json
{
  "version": 1,
  "apps": {"default": "allow", "deny": ["keepass.exe", "1password.exe", "*.exe"]},
  "tools_denied": ["run_script", "kill_app", "clipboard_read", "start_recording", "replay_trajectory"],
  "clipboard": {"read": "deny", "write": "allow"},
  "files": {"write_allow": ["%USERPROFILE%\\Documents\\**"]},
  "foreground": "allow",
  "max_runtime_sec": 600
}
```

`clipboard_write` 允许，因为逐字符输入大段文本既慢又容易丢字；`clipboard_read` 仍禁止，避免把用户剪贴板里的密码之类读进模型上下文。写入后覆盖前一份内容，不读回、不清空复原。

这是策略约束，不是安全边界。Windows 上 bounded 模式不可用（授权应用匹配不上、启动检查 fail-open），真正的隔离只能靠 standard 模式 + 用户态自觉。文档里必须这么写，不要让人误以为是沙箱。

`foreground` 取值 `allow` / `deny`，与 CLI 参数冲突时取更严格的一方。

---

## 9. health.py

```json
{
  "contract": 1,
  "ok": true,
  "driver": {"version": "cua-driver 0.38.0", "running": true, "permission_mode": "standard", "binary": "C:\\..."},
  "model": {"adapter": "openai-compatible", "reachable": true, "name": "local", "probe_latency_ms": 140},
  "smoke": {"session": "cu-health-20261025T041500-b2c3d4", "list_windows": "ok", "get_window_state": "ok", "token_fresh": true},
  "overlay": {"available": true},
  "python": "3.12.4",
  "warnings": []
}
```

- 必做冒烟：带 session 做一次 `list_windows` + `get_window_state`，专门验证 token 不 stale。
- 模型探测只发一次空请求，验证端点可达和返回可解析，不验证质量。
- 按 §1 部署修订自动启动所需 driver，再做只读桌面探测；启动失败或已有 daemon 不兼容时返回 `ok: false`，warnings 说明原因。
- exit 0 / 1。

## 10. stop.py

```
stop.py [--session ID] [--all] [--driver] [--json]
```

- 无参数：停掉最新一个在跑的 run。
- 作用：写 `stop.flag`，等 5 秒；没退就杀 wrapper 进程树。
- `--driver` 额外停掉驱动 daemon。
- 输出 `{"contract":1,"ok":true,"stopped":["cu-..."],"forced":false}`。
- 这个脚本只给人和 `health.py` 用，agent 正常情况下不调。

---

## 11. 明确不做

- 不做常驻服务、不做 REPL、不做跨 run 的状态缓存。
- 不在磁盘上留任何运行痕迹（除 `--keep-artifacts`）。
- 不按应用写脚本，不存在 `open_excel.py` 这种文件。
- 不做意图分类或路由，不预判"这个意图归哪个模型"。
- 不在 wrapper 里做多轮对话。
- 不实现 bounded 模式开关。
- 不做屏幕上的元素定位新算法，坐标和元素 id 一律用驱动给的。

## 12. 未决

1. 多显示器与高 DPI 下 `frame` 的坐标系归属，等有双屏环境再验。

原列的另两条已定：截图改成按需触发（§6.2），守卫被强杀后由下一次 `run.py` 启动时的兜底清扫回收（§5），不另设看门狗。
