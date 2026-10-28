# 模型接入

外部请求和响应遵循 [contract.md](contract.md) 的 B 层。所有脚本以 skill 根目录加载配置，使用计算机环境中的 Python；HTTP worker 和守卫子进程继续使用同一 sys.executable。模型端点缺失或返回不可解析 JSON 会立即 error/model_unavailable；不解析 Markdown 代码围栏或自然语言。

## HTTP 模型

复制 model.example.json 到 model.json。endpoint 是完整 `/v1/chat/completions` URL；model 是服务实际暴露的模型名。OpenAI compatible 请求包含单条 user message：没有截图时 content 是 B 层 JSON 字符串；有截图时 content 为 text 与 image_url 两项，image_url 为 PNG data URI。本地文件路径仍保留在 JSON，图像内容同时传给服务。`stream:false`，默认 `response_format:{type:json_object}`；服务不支持 JSON mode 时可在配置中加 `json_mode:false`，完整 JSON 响应要求不变。

可选 `api_key_env` 指定持有 API key 的环境变量名，配置里不保存密钥。HTTP 不自动跟随重定向，避免将界面内容意外发送到其他端点。请求在独立短生命周期进程内执行，可被 ESC、stop 和总超时打断。

## 命令模型

command 是 argv 数组，不经过 shell；子进程 stdin 收到一份 B 层 JSON，stdout 必须仅输出完整响应 JSON。日志放 stderr。非零退出码视为模型错误。wrapper 自身从不读 stdin，只有模型子进程的管道传入请求。

command 模型应在自身提示模板中实现冻结契约，包括把 UI 文字当作数据、遵守 policy 可选字段、确认不可逆动作和支持独立验证请求。health 使用一次空对象 `{}` 探测，只检查响应为可解析对象。

## 观察和动作

observation 含 elements 或 tree_markdown，默认 elements。可选配置 tree_format:"markdown" 切换有 markdown 时的输入。history 最近 8 轮；policy 是附加上下文。text 实例永不生成截图。vision 仅按需拍摄且单 run 最多 30 张。

元素 target 只有 element_token；坐标 target 只有 x/y，值为屏幕绝对物理整数像素。截图若提供可靠 screenshot_origin，可用图内坐标加原点得到屏幕坐标，不能把截图像素直接当屏幕点。原生补丁保留屏幕坐标到实际输入，不依赖截图缓存；text 模型坐标输入不会触发截图。drag 的 from_x/from_y/to_x/to_y 也使用屏幕坐标。

最多 5 个动作；actions、done、ask_human 互斥。允许词表见冻结契约。模型不能覆盖 session、驱动权限、截图路径或原生脚本。多窗口时先以 `get_window_state` 的 args.window_id 选择一个已枚举的窗口；不要凭名称猜 HWND，也不要把一个窗口的 token 用在另一窗口。

模型 get_window_state 的 query/max_elements/max_depth/full_output 会在本次窗口后续全量观察与独立验证中保留，可用于读取默认范围之外的元素。新窗口列表始终经 --app 与策略过滤后提供。need_vision 连续三轮为 true 时第三轮请求被忽略并警告。只有 expect.tool 为 get_desktop_state 时可以请求全屏截图；此项仅观察请求，不是额外允许动作。

## 可检查的完成证据

原契约的最小 evidence（kind、detail，可选 element_token/window_id）保持有效：缺少确定性比较字段时，重新观察后使用无规划历史的独立模型请求验证完整意图。以下附加字段仍为可选，用于省去该次模型调用：

| kind | 附加字段 | 校验方式 |
|---|---|---|
| readback | element_token、expected；可选 property（默认 value）、match（exact/contains） | 从新状态重新定位元素，确定性比较属性 |
| state_change | property（window_title/app/window_id）、expected | 新观察与旧观察不同，且等于期望 |
| dialog_gone | window_id | 该窗口此前确实被观察，重新枚举后消失 |
| screenshot | detail；可选 window_id | 新截图独立调用模型，移除规划历史，检查完整意图 |

例如：

```json
{"done":{"summary":"标题已更新","evidence":{"kind":"readback","detail":"重新读回标题为项目计划","element_token":"s00000010:2","property":"value","expected":"项目计划","match":"exact"}}}}
```

读回属性必须由新观察实际提供；缺失 value 是未知，不能当作 null 或空字符串。in_web_content:true 的 UIA value 可能只是写入回显；label_from_value:true 表示 label 也来自该值，两者都不能证明应用接收了内容，应改用截图或其他可信状态。空字符串只有在原生确实返回 value:"" 时可比较。成功关闭窗口后的尾部观察错误不会重放动作，完成仍需重新枚举验证 dialog_gone。

独立校验不通过时 last_error.code 为 verification_failed；只再给一个修正决策轮。该轮可以返回带新证据的 done，或返回修正 actions。动作后按原失败证据立即复验，可选 expect.evidence 可以替换证据。再次失败返回 blocked/verification_failed。

独立校验请求仍保留 B 层必需字段，增加 verification 对象（含声明和动作前基线），并清空规划 history。响应继续遵守 B 层：仅在新观察证明完整目标时返回 done/evidence，否则返回 ask_human/needs_decision 说明未证实之处，不执行任何动作。每次独立模型请求都计入 steps_used 和 --budget；耗尽时不会再发校验请求。总超时和截图上限同样适用。command 模型也需识别 verification 上下文。

所有 driver 工具调用共享本次 session；agent 不需要也不应直接调用驱动。driver 批处理可能在上游压缩原始单步文本，详见 driver-038.md。
