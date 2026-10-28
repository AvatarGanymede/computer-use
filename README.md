# computer-use

Windows 桌面自动化 skill，按 `computer-use skill 接口契约 v1` 实现。入口是 `scripts/run.py`、`scripts/health.py` 和 `scripts/stop.py`，外部调用只需解析一个 JSON 对象。

## 安装

需要 Windows 11（或支持 WDA_EXCLUDEFROMCAPTURE 的 Windows 10 2004+）、Python 3.12+、本项目基于 Cua Driver 0.38.0 的原生补丁，以及可接收完整 JSON 的本地模型。普通上游二进制缺少必要能力，不能直接代替。

将本目录复制或链接到 `~/.claude/skills/computer-use`。也可直接从开发目录调用；脚本相对于自身所在目录加载配置。使用计算机环境中已有的 Python 3.12+，skill 不附带 Python 或 venv。以下命令在 skill 目录运行，`python` 也可换成 `py -3` 或已确认版本的环境解释器绝对路径，所有命令保持使用同一个解释器。

检查运行依赖：

```bash
python -c "import sys, PIL; print(sys.executable); print(sys.version); print(PIL.__version__)"
```

如果该解释器缺少 Pillow，再安装声明的依赖：

```bash
python -m pip install -r requirements.txt
```

复制 `model.example.json` 为 `model.json` 并填写实际模型端点/模型名；命令模型可参考 `model.command.example.json`。不附带预选模型，不会自动下载模型，不会在配置缺失时降级。

仓库自带补丁版发行目录 `native/bin`，包括 driver、UIA helper 及所需 DLL，发现优先级高于系统安装；也可用 `CU_DRIVER_BINARY` 指定。不要只复制单个 exe。补丁源码与构建方法不随本仓库发布。

调用 `run.py` 或 `health.py` 时，wrapper 先检查已有 daemon，未运行就自动在后台启动补丁版 `cua-driver.exe serve --permission-mode standard`。启动最多等待 15 秒并计入本次总超时；并发调用串行完成启动检查。已有 standard daemon 通过能力校验后复用，其他模式或能力不符会明确报错，不自动替换或重配。无需用户另开终端维护 serve。

运行健康检查（会启动所需 daemon，并进行模型、只读窗口/token 和守卫探测）：

```bash
python scripts/health.py --json
```

策略可复制 `policy.example.json` 为 `policy.json`。示例不包含契约演示中的 `*.exe`，因为那会拒绝所有应用；如确需全拒绝，可自行加入。deny 优先，后台限制和 foreground 策略取更严格的一方。

## 使用

```bash
python scripts/run.py "读取当前记事本文档的标题" --app notepad.exe --json
```

```bash
python scripts/run.py "将记事本文档的首行改为项目计划并确认读回" --app notepad.exe --background-only --json
```

```bash
python scripts/stop.py --all --json
```

默认超时 120 秒、30 次模型决策，上限分别为 600 秒和 100 次。`--no-overlay` 同时禁用前台。默认 run 结束删除全部临时文件；只在排查时加 `--keep-artifacts`，`--artifacts-dir <目录>` 自动保留到该目录下本次 session 子目录。清理失败会写入 warnings，后续启动清扫超过 1 小时的非保留、非活动残留。stop 也会清扫。

模型与驱动 CLI 的标准输出被捕获，wrapper 日志写 stderr。后台 daemon 的标准流定向到 DEVNULL；正常结束只清理本次 session，daemon 留给后续调用复用，可用 `python scripts/stop.py --driver --json` 显式关闭。状态和退出码：done=0、error=1、blocked=2、needs_human=3、timeout=124。所有需要人决定的结果返回 needs_human，不读取 stdin。

## 结构

- `SKILL.md`：agent 触发、调用和退出分支。
- `scripts/cu/runtime.py`：观察、决策、执行、独立校验。
- `scripts/cu/driver.py`：Cua Driver 0.38.x 的 session、批处理、坐标与错误适配。
- `scripts/cu/model.py`：OpenAI compatible/command 模型传输。
- `scripts/cu/guard*.py`：Windows 覆盖层与 ESC 守卫。
- `references/contract.md`：contract 1 与 2026-10-27 部署修订（系统 Python、按需自动启动）。
- `references/driver-038.md`：固定驱动 schema、补丁能力及验证边界。

## 模型输入与完成证据

详见 [references/model-protocol.md](references/model-protocol.md)。vision 模式按需截图，最多 30 张；text 模式永不截图。OpenAI compatible 将契约 JSON 放在单条 user message；有截图时附上标准 image_url data URI。图片路径不暴露给不能访问本地文件的 HTTP 服务作为唯一视觉输入。command 模型的 stdin 收到同一契约对象，它自行读取 screenshot_path。

完成必须由新观察独立验证，不能只接受模型的 done。带可选 expected/property 的 readback/state_change 使用确定性比较；dialog_gone 重新枚举确认。原契约的简略证据与截图证据使用无规划历史的独立模型请求；这些请求和规划请求共享 --budget、steps_used 与总超时。无法证明时返回 verification_failed，预算不足则返回 budget_exhausted。

## 限制

这是策略约束，不是安全边界。Windows bounded 模式在目标版本不可用，standard 不是沙箱；GUI 中隐含的文件写入和剪贴板操作仍需模型遵守策略。此 skill 不读剪贴板、不恢复剪贴板旧内容、不提供任意脚本执行。

驱动 0.38.x 的原生批处理会压缩单步错误文本、丢弃部分 effect 信息。wrapper 保留收到的完整原始输出，但不能恢复上游已经丢弃的内容。原生 refound 重试计入一次上限；wrapper 自己重试时改为单动作原生调用，避免再次触发批处理自动重试。跨 run 不缓存窗口和结论。

原生补丁在 admission 和输入单元之间检查会话取消。wrapper 绑定自己的 PID/创建时间和 Windows 事件，每次执行重核对通道，并给派发携带首次绑定的事件身份；daemon 重启不能让新会话接受旧通道的动作。中止先发事件再回收 CLI，并另以 end_session 等待实际 worker 清理；CLI 传输失败时事件仍可停止后续输入，wrapper 意外退出也会触发原生 owner 检查。未确认清理会输出 driver_stop_unconfirmed。已投递的输入不能撤回，已进入的 Win32/COM 调用不能回滚；允许释放键、按钮和触点。stop 强杀前核对进程创建身份，避免 PID 重用误杀。

真正的独占全屏不能保证覆盖层可见；守卫检测到系统 Direct3D 全屏信号会先提示，ESC 仍负责中止。此信号不保证识别所有渲染后端。多显示器/高 DPI 坐标仍需对应设备实测，不能将单屏测试当作已覆盖。

health 使用新增的只读 probe_element_token，核对同 session 的语义 snapshot、精确 PID/HWND 和 retained UIA/MSAA 属性可读性；不会通过点击或新 token 字符串假报新鲜。原生绝对物理坐标路径不依赖截图缓存，也不会让 text 模型为坐标输入截图。

点击脉冲由透传鼠标钩子观察带标记的原生 mouse-down 开始计时，不以 CLI 返回作为点击时刻。Windows 不提供可靠 API 查询低级 hook 后续被系统静默移除；守卫持续检查心跳和已知能力，支持普通桌面，不支持安全桌面。当前未进行补丁版实机联调。本机健康检查报告真实依赖状态；只有模型与驱动均配置完成，才能进行实际 GUI 的端到端验证。
