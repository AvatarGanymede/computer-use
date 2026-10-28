# Cua Driver 0.38.0 adapter

适配器使用 Windows、`standard` daemon 和本项目的固定版本原生补丁。上游基线为 `trycua/cua` 的 `cua-driver-rs-v0.38.0`，commit `85e2b1318bd77ccf3cfa88ccdf875a4bb7eaed19`。补丁源与构建方法不随本仓库发布；发行文件见 `native/bin`。wrapper 在 run/health 初始检查时按需启动 daemon，不切换已有 daemon 的权限模式。Windows standard 和 wrapper 策略不是沙箱。

## CLI 与发现

`CU_DRIVER_BINARY` 优先于构造参数 `binary`；随后查 skill 的 `native/bin`、当前环境 Python 所在目录、PATH、`~/.local/bin`、`~/.cargo/bin`。显式路径失效不会回退；不查找项目 venv，源码没有任何 Temp 安装位置。

调用是 `[binary,"call",tool,json.dumps(args)]`，JSON 是位置参数，不是 stdin，也没有 `--json`。每次 call 都注入同一个 session。stdout 是业务 JSON；`Failure.raw` 完整保留收到的原始 stdout。`inspect()` 执行 `--version` 和 `status`；只支持 `cua-driver 0.38.x`，且每次 call 前必须从 status 明确读到 `permission mode: standard (...)`。没有权限行、daemon 关闭、bounded 或 unrestricted 均拒绝。

`run.py` 和 `health.py` 在首次使用前调用 `ensure_started()`，保持 `inspect()` 只读。无 daemon 时取得 Global 命名 mutex，跨 Windows 登录 session 串行重新检查；仍未运行才用当前环境 Python 的短生命周期启动器运行 `serve --permission-mode standard`。启动器先原子创建 Global 生命周期事件，按原生 canonical executable 的 release/local endpoint 命名空间区分；已有标记时等待原实例，不再次 spawn。新 daemon 经显式 handle_list 继承标记，其持有时间覆盖进程创建至 pipe 就绪及整个运行期，wrapper 取消/超时/退出不能释放 daemon 持有的副本。最后一个持有进程退出才由内核回收标记，无持久标记文件。启动器退出后 daemon 不再是存活 wrapper 的子进程；stdin/stdout/stderr 均为 DEVNULL，不继承捕获管道。等待 mutex、启动器和 status 就绪合计最多 15 秒，并服从原调用的期限与取消。启动器中止只终止启动器本身，不杀可能已供其他调用共享的 daemon；失败或中止后已经启动的空闲 daemon 可留待后续调用复用。正常结束只清理 session，显式 `stop.py --driver` 才关闭共享 daemon。

Global mutex/event 访问失败会拒绝启动，不回退到不同锁；不同用户对同一机器级 endpoint 的访问仍由原生 ACL 控制。生命周期标记若被其他后代继承而晚于 daemon 退出，后续调用会保守等待并在 15 秒内报错，不越过标记创建重复实例。已检查的原生直接应用启动禁用 handle 继承，内部 shell helper 使用自己的白名单；所有真实继承与跨登录 session 行为仍待实机验证。DETACHED_PROCESS 不代表逃离 Windows Job Object；宿主若使用关闭即终止的 job，关闭宿主仍可能结束共享 daemon，后续已绑定会话会拒绝继续，不宣称进程生命周期完全独立。

每个 Driver 最多尝试启动一次；session 已建立或取消事件已绑定后禁止自动重启，普通 call/取消/end_session/stop 不经过启动路径。已有其他权限模式、不可达 endpoint 或不兼容能力均拒绝，不停止或重配已有实例。

运行前查询 `computer_use_capabilities`，要求 revision 1，且 `session_input_cancellation`、`read_only_token_probe`、`screen_physical_coordinates`、`direct_input_cancellation_event`、`cancellation_owner_process`、`cancellation_event_continuity` 全为 true。普通上游 0.38.0 缺少这些能力，会清楚报错，不会继续桌面输入。不要仅用 CLI 版本号判断补丁可用。

## 取消与会话

每个 run 使用一个非保留的公共 session 标签，原生 registry 将其映射为可信的私有 lifecycle ID。派发上下文经 Tokio task-local 和 blocking worker thread-local 传播，子 worker 持有 dispatch lease；异步调用超时不等于实际 UIA worker 已退出。

wrapper 在每次执行动作前调用 `bind_input_cancellation`，传入自己的 PID 与 Windows `GetProcessTimes` 创建 FILETIME，核对返回的精确身份并以 `EVENT_MODIFY_STATE` 打开 `Local\\cua-driver-cancel-<UUID>` 手动复位事件。首次事件名固定到本 run，后续绑定返回不同事件便拒绝继续；每次绑定后的派发还携带 wrapper 专用的 `input_cancellation_event`，使绑定与动作之间发生 daemon 重启也不能接收旧通道的输入。该字段不进入模型参数或平台 schema。原生在取得 dispatch lease 的同一 lifecycle 锁中核对现存事件、transport 与活 owner，缺失或改变时不会新建会话，nested batch/retry 继承同一期望；`input_cancellation_mismatch` 映射为不可重试的 driver_unavailable。原生持有该事件和经过身份验证的 wrapper 进程 handle，在 admission 与输入单元之间检查事件或进程退出；信号一旦收到不允许复位或绑定其他 owner。没有额外看门狗或取消文件。

ESC、stop、总超时、守卫失效和驱动 CLI 异常先 SetEvent，再回收 CLI 子进程，避免慢 taskkill 阻挡原生停止信号。模型传输中的停止也会发信号；命令模型的全部管道读写位于工作线程，主线程继续检查期限和 stop，兼容 Windows Python 3.12 的同步 stdin 写入行为。

收尾在独立的 5 秒预算内调用同 session 的 end_session，只有明确 `active:false` 且没有未完成清理时才标记 `stop_confirmed`。已送达的事件停止后续输入，不等同于所有 COM worker 已退出。未确认清理会产生 `driver_stop_unconfirmed`，不能宣称成功；已有用户中止或失败原因保留。若事件和 CLI 都未送达，则守卫留到 wrapper 退出，原生输入同时检查持有的 owner 进程 handle。默认运行文件仍删除。共享 daemon 不会被当作取消手段强杀。

取消是协作式的：停止后续字符、鼠标移动、滚轮 tick、按下、UIA 变更，以及等待查找后的应用启动和窗口变更。内部 shell helper 继承原 event、wrapper 和 daemon 的只同步 handle，在实际 ShellExecute 前检查，父 worker 持有 lease 至 helper 退出；允许释放已按下的键、按钮、触点及必要收尾。已经投递的输入不能撤回，已经进入的 Win32/COM 调用不能强行回滚。这些调用的 worker lease 保持到实际返回，不能把超时当作静止。

## Python 接口

- `Driver(session,check,remaining,binary)`：构造不启动 daemon；首次使用检查版本。
- `ensure_started()`：仅 run/health 初始检查调用；按需启动、等待就绪并确认 standard，能力校验仍由 require_capabilities 完成。
- `call(tool,args)`：注入 session、检查状态、解析业务 JSON、规范错误。
- `require_capabilities()` / `bind_input_cancellation()`：验证原生扩展并绑定本次 wrapper 生命周期；模型不能调用或设置这些内部控制。
- `request_stop()`：发不可复位的停止信号，不等待 CLI；`cancel(timeout=5)` / `close(timeout=5)` 另行确认 worker 清理。
- `schema`：从真实 `describe <tool>` 读取冻结词表 schema，深拷贝给模型；移除内部 `coordinate_space` 与 `input_cancellation_event`，模型继续只使用冻结的屏幕 target。
- `list_windows()`：规范 app/window_title/window_id/pid，优先 Win32 进程路径确认可执行名，不根据窗口标题猜应用。
- `observe(window,screenshot=None,screenshot_only=False,desktop=False,options=None)`：默认全量元素树，始终省略 since；只继承模型明确请求的 query/max_elements/max_depth/full_output。截图必须显式提供路径，screenshot_only 不重建语义 token；只有显式 desktop 请求才观察桌面。
- `probe_token(window,token)`：调用只读 `probe_element_token`，同时核对返回 token、PID、HWND 和 token_fresh。
- `validate_actions(actions,windows_for_actions,foreground_for_actions)`：整轮预校验，不派发输入或移动光标。
- `execute(actions,window,foreground=False,retry_allowed=True)`：仅在预校验和取消绑定通过后执行，返回 ok/completed_count/results/action_retries_used；模型不能覆盖会话、目标窗口、截图路径、scope、capture_id 或自动前台降级。

## 批次与观察

只把 click/double_click/right_click/set_value/type_text/press_key/hotkey/scroll/drag 放入 run_steps；launch_app/invoke_menu/verify_state/get_window_state/list_windows 按原顺序单独派发。move_cursor 虽是原生能力，不在冻结词表中。每批显式设置 delivery_mode 和 foreground_fallback:false。末批可带 `observe:{since:null,include_screenshot:false,tree_format:"elements",max_image_dimension:0}`；空 observe 的默认 since=latest 在上游会触发 typed_output_mismatch。

run_steps 的 index/failed_step 从零开始；executed 包含失败和跳过项，不能当作成功数。wrapper 依连续成功行确定 completed_count，只重试未完成后缀。expect 阶段失败表示动作已执行，禁止重放。所有动作成功但尾部观察失败时，保留该观察错误作为数据，下一轮全量观察和完成验证判断结果；因此成功关闭窗口后仍可走 dialog_gone 校验。

原生 refound 消耗该动作唯一一次重试；wrapper 自己重试时使用单动作接口，避免第二次原生隐式重试。跨 HWND 或 PID 改变时不向其他窗口重放。

模型显式 get_window_state 的扩展/查询参数保存在本次窗口上下文，后续规划和独立校验用相同参数重新全量读，因此目标位于默认 250 个元素之外时仍可读到。完整原生结果写 transcript，模型 history 保持最近八轮紧凑摘要，不重复整棵树或未获允许的窗口信息。

## Token 与读回

只读 probe 要求可信 session 所有权、精确 PID/HWND、完整 snapshot:row、语义 snapshot 及实际 retained UIA/MSAA 对象属性读取。读取前后再次核对 snapshot 未被替换或回收；不 refind、不 focus、不 invoke、不发布新树或截图。截图刷新只改变 screenshot_owner，不能转移 semantic_owner。失败、超时或跨 session 都不能返回 token_fresh:true。

normalize_state 保留 value 的真实存在性；缺失值不补成 null。原生 ValueValue 读取先确认 ValuePattern 支持、禁用 UIA 属性默认值，仅实际 BSTR 保留空字符串与空白；缺失、不可读和其他 variant 类型保持未知。in_web_content 与 label_from_value 标记保留给模型与校验器，值衍生标签不能绕过回显检查。Chromium/Electron 的 UIA ValuePattern 可能仅回显写入而未传到渲染器，不能用该 value 证明应用已接收；需要截图或其他可信状态。verify_state 只有 status:satisfied 且 stable:true 才继续。

## 绝对坐标与守卫

target.x/y 与 drag 起终点保持屏幕物理整数像素原值，wrapper 添加内部 coordinate_space:screen_physical_pixels，原生在严格校验后转换为私有标记。该路径绕过截图缩放和 bitmap 原点，不依赖 screenshot 缓存，不为坐标输入额外截图；text 模型也可用坐标。模型不能伪造私有标记。后台 wheel 投递到精确窗口子树，检查 Win32 打包范围；不能用忽略坐标的滚动替代。

坐标默认先尝试后台，只有 background_unavailable 且 CLI/策略允许时才启动守卫转前台。守卫在完成倒计时和移动 ACK 后才允许输入。点击脉冲由透传 WH_MOUSE_LL 观察真实的、带本项目标记的 native mouse-down，在精确目标点开始计时；光标移动 ACK 和 driver 返回都不产生脉冲。守卫不注入点击或键盘输入。

## 验证边界

离线构建、纯单元测试与独立审查已完成，具体记录不随本仓库发布。未启动补丁 driver、模型、真实覆盖层或钩子做联调。UIAccess helper 的签名/可信部署要求、真实 ESC 延迟、独占全屏、跨显示器与高 DPI 仍需获准后实测。Windows 无可靠 API 查询低级 hook 后续被系统静默卸载；安全桌面不支持。

上游 run_steps 仍会裁剪 message 且省略部分 effect/route/refusal 信息；wrapper 忠实保留收到的 raw，不能恢复原生丢掉的数据。ok 从不替代完成独立验证。上游基线：[session](https://github.com/trycua/cua/blob/85e2b1318bd77ccf3cfa88ccdf875a4bb7eaed19/libs/cua-driver/rust/crates/cua-driver-core/src/session.rs)、[Windows tools](https://github.com/trycua/cua/blob/85e2b1318bd77ccf3cfa88ccdf875a4bb7eaed19/libs/cua-driver/rust/crates/platform-windows/src/tools/impl_.rs)。
