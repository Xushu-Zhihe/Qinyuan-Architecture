# Blackboard Result 协议 Skill 与纠错重试设计

## 背景与根因

项目 `project-82d8a2bcc4724a6ebc2ad9ddee3a235c` 的两次真实 execution 都已找到同一个 flag，但最终答案没有进入黑板：

- 第一次输出使用 Python 风格 `key=value`，不是合法 JSON，解析失败为 `Expecting value: line 1 column 1 (char 0)`。
- 第二次输出是合法 JSON，但把 `facts` 写成字符串数组、把 `edges` 写成二维数组，解析失败为 `fact proposal must be an object`。

当前 `render_execution_request()` 只列出顶层字段名，没有说明 `facts` 与 `edges` 的对象结构、允许的关系类型、临时 ref 规则或可直接复制的完整示例。执行 Agent 因此知道“必须返回哪些字段”，却不知道解析器真正接受的 schema。Supervisor 只会在 `parse_result()` 成功后登记 Fact 和 final answer；协议错误时，即使自然语言输出已经包含 flag，也不能安全地把 goal 标记为完成。

## 目标

- 让 Codex execution Agent 在首次尝试中稳定生成符合 `parse_result()` 的 `BLACKBOARD_RESULT`。
- 保留严格、事务性的协议边界，不从任意自然语言或正则匹配的 flag 推断 goal 已完成。
- 当 provider 已完成工作但结果协议无效时，针对同一个 Intent 自动执行最多一次纠错重试，不重新规划新的 Intent。
- 保留每次 provider 调用独立的 execution、stdout、stderr、状态和错误信息。
- 让 Codex 可以通过项目内 Skill 获取协议说明和确定性验证工具，同时保证不支持 Skill 的 provider 仍能仅凭 prompt 正确输出。

## 非目标

- 不让解析器兼容字符串形式的 facts、二维数组 edges 或 Python 风格 key/value 文本。
- 不扫描自然语言中的 `flag{...}` 并绕过结构化结果协议。
- 不改变最终答案的多 provider 共识门槛。
- 不把 Codex Skill 作为 Claude Code、Kimi 等 provider 的通用插件机制。
- 不修改已有失败项目的 SQLite 数据；修复只影响后续 execution。

## 方案比较

### 方案 A：只放宽解析器

接受字符串 facts、二维数组 edges，并尝试猜测 ref 与关系类型。该方案可以吞下本次第二种输出，但会引入歧义，削弱事务性协议，也无法可靠处理第一次完全非 JSON 的结果，因此不采用。

### 方案 B：只强化 prompt

在 execution prompt 中写出完整 schema 和示例。该方案改动最小，能显著降低失败率，但 provider 仍可能偶发输出格式错误，失败后当前循环会重新进入 planning，产生语义重复的新 Intent，因此不足以完整解决问题。

### 方案 C：完整契约、项目 Skill、严格解析、一次原 Intent 纠错重试

这是选定方案。Prompt 是所有 provider 的基础契约；Skill 为 Codex 提供可复用说明与验证脚本；严格解析器继续充当最终边界；Supervisor 在协议错误时保留失败 execution，并为同一个 Intent 创建一个新的纠错 execution。第二次仍失败才把本轮返回为失败。

## Agent 输出契约

`render_execution_request()` 必须直接包含以下规则，不能只依赖 Skill：

- 显式写出 `Codex: use $blackboard-result-protocol before producing the result`；Skill 不可用时继续遵循同一 prompt 内联契约，不能中止任务。
- 最后一行必须以 `BLACKBOARD_RESULT: ` 开头，后面紧跟单行、严格合法的 JSON object。
- 顶层必须且只能包含 `intent_id`、`facts`、`edges`、`goal_achieved`、`final_answer`。
- `facts` 中每项必须是 `{"ref":"temporary-ref","content":"non-empty fact"}`。
- `edges` 中每项必须是 `{"type":"supports","from":"node-or-temp-ref","to":"node-or-temp-ref"}`。
- `type` 只能是 `supports`、`contradicts`、`derived_from`、`addresses`、`similar_to`、`equivalent_to`。
- 临时 fact ref 在单次结果内必须唯一；edge 可以引用现有节点 ID 或同一结果中的临时 fact ref。
- `goal_achieved` 必须是 boolean；为 `true` 时 `final_answer` 必须是非空字符串，否则可以是 `null`。
- 即使没有 fact 或 edge，也必须输出空数组。
- 输出前必须验证 JSON 和结构；不要输出 Python dict、键值语法、tuple 或 Markdown code fence 作为协议行。

Prompt 提供一个包含 fact 和 edge 的完整示例，而不是只提供空数组示例：

```text
BLACKBOARD_RESULT: {"intent_id":"intent-123","facts":[{"ref":"flag-fact","content":"flag = flag{example}"}],"edges":[{"type":"supports","from":"flag-fact","to":"goal-123"}],"goal_achieved":true,"final_answer":"flag{example}"}
```

示例中的 intent 与节点 ID 明确标注为占位值，Agent 必须替换为 Assigned intent 和 Graph snapshot 中的真实 ID。

## 项目内 Skill

新增 `.agents/skills/blackboard-result-protocol/`，使从仓库子目录启动的 Codex CLI 能发现协议 Skill。Skill 名为 `blackboard-result-protocol`，触发条件覆盖执行 blackboard Intent、生成 `BLACKBOARD_RESULT`、出现 `protocol_error`、`fact proposal must be an object`、missing protocol payload 等场景。

Skill 内容保持低自由度：

- 重复列出唯一允许的 JSON schema、关系类型和 ref 规则。
- 要求先在工作目录生成待提交的 JSON 文件，再运行验证脚本。
- 验证成功后，才把文件内容压成单行并加上 `BLACKBOARD_RESULT: ` 前缀。
- 明确禁止根据“已经找到 flag”跳过验证。
- 包含一个正确示例和本次两种错误输出的反例。

新增 `scripts/validate_result.py`，复用项目的 `orchestrator.protocol.parse_result()`，避免 Skill 与生产解析器各自维护不同规则。脚本接收：

- `--intent-id`：Assigned intent ID。
- 一个或多个 `--existing-node-id`：结果 edge 使用的现有节点 ID。
- `--file`：仅包含 JSON object 的 UTF-8 文件。

脚本为 payload 加上协议前缀后调用 `parse_result()`；合法时退出码为 0 并输出 `valid`，不合法时退出码为 1 并把解析错误写到 stderr。

Skill 是 Codex 的增强层，不是系统正确性的唯一依赖。非 Codex provider 仍通过 execution prompt 获得相同完整契约。

## 协议纠错重试

`ProjectSupervisor._execute_intent()` 对 provider process 成功但 `parse_result()` 返回 `protocol_error` 的情况执行以下流程：

1. 将当前 execution 标记为 `protocol_error`，保留原 stdout、stderr 和具体错误。
2. 如果尚未使用纠错机会，为同一个 `IntentRecord` 创建新的 execution。
3. 新请求包含完整基础契约、前一次解析错误，以及前一次最后一条 `BLACKBOARD_RESULT` 协议行；要求只修正结构，不创建新 Intent。
4. 第二次结果通过现有严格解析器和 `finish_execution()` 事务入库。
5. 第二次仍为协议错误时停止纠错，把 `run_agent_once()` 返回为 failed；后续 failure threshold 行为保持不变。

纠错次数固定为一次，不放入 provider 配置，避免无限重试和额外配置面。Provider process 的 unavailable、timeout、cancelled 或非零退出不触发协议纠错，因为这些情况没有可验证的成功输出。

每次尝试都创建独立 execution，不能复用同一日志路径或覆盖第一次日志。Intent edge 的聚合状态沿用现有规则：任一 execution completed 后 Intent 为 completed；否则失败尝试全部结束后为 failed。

## Goal 停止语义

Goal 的停止条件保持为“结构化结果成功入库并达到现有共识”，而不是“日志中出现类似 flag 的文本”。成功纠错后：

- `finish_execution()` 记录 Fact、普通关系和 final answer proposal。
- `consensus()` 达到门槛时项目状态改为 `achieved`。
- Supervisor 设置 `cancel_event`，其他 provider 的活动 execution 按现有逻辑取消。

这样既解决“已找到答案却因格式问题继续规划”的现象，也不允许恶意或偶然日志文本绕过黑板协议。

## 文件变更

- 新增 `.agents/skills/blackboard-result-protocol/SKILL.md`：Codex 协议工作流与反例。
- 新增 `.agents/skills/blackboard-result-protocol/agents/openai.yaml`：Skill 展示元数据和默认调用提示。
- 新增 `.agents/skills/blackboard-result-protocol/scripts/validate_result.py`：调用生产解析器的确定性校验工具。
- 修改 `orchestrator/protocol.py`：集中生成完整结果契约；支持包含前次错误和被拒协议行的纠错请求。
- 修改 `orchestrator/supervisor.py`：同一 Intent 最多一次协议纠错 execution。
- 修改 `tests/test_protocol.py`：锁定 prompt 中的精确 schema、关系类型、正确示例和纠错上下文。
- 修改 `tests/test_supervisor.py`：锁定首次协议失败、同 Intent 重试、独立日志、成功停止及二次失败行为。
- 新增 `tests/test_blackboard_result_skill.py`：验证 Skill 元数据和校验脚本的成功/失败退出行为。
- 修改 `README.md`：展示包含对象 facts/edges 的完整协议示例，并说明一次纠错重试。

## 测试策略

### RED 基线

现有保存日志已经证明缺陷：

- 非 JSON 的 `BLACKBOARD_RESULT` 被拒绝。
- 猜测的字符串 facts/数组 edges 也被拒绝。
- 由于 prompt 没有 schema，Agent 连续两次未能自行推导正确格式。

代码实现前还要增加自动化失败测试：

- `render_execution_request()` 当前不包含 `{"ref":...,"content":...}` 和 edge object 契约。
- Supervisor 当前在首次 `protocol_error` 后直接返回 failed，不会为同一 Intent 创建纠错 execution。

### GREEN 验证

- 协议 prompt 测试确认显式 Skill 调用、完整字段结构、允许的 edge type、正确示例和不可用回退说明都存在。
- Skill 校验脚本接受正确对象格式，拒绝非 JSON、字符串 fact、未知 ref 和 goal 无 answer。
- Supervisor 测试确认首次错误、第二次正确时只创建一个 Intent、两个 execution，并最终达到 `achieved`。
- Supervisor 测试确认两次协议错误后返回 failed，两个 execution 均保留各自错误与日志。
- 完整 Python 测试套件通过。

### Skill 前向验证

受当前会话禁止派生 subagent 的约束，不启动额外 Agent。使用已保存的两次真实失败输出作为无 Skill 基线；实现后用 Skill 校验脚本和 provider prompt 的自动化测试验证约束。后续在可隔离的测试环境中，可再用真实 Codex CLI 执行一次不泄露答案的协议任务作为部署前验证。

## 错误处理与可观测性

- 第一次 protocol error 仍发布现有 `intent.execution_finished` 事件，状态为 `protocol_error`。
- 纠错 execution 使用新的 execution ID 和工作目录，前端可以分别查看两次日志。
- 纠错 prompt 包含解析器原始错误文本，便于 Agent 精确修正。
- 不新增数据库 schema；现有 `intent_executions` 已支持同一 Intent 多次 execution。
- 不把第一次错误改写为 completed，也不隐藏失败历史。

## 成功标准

- 本次第二次尝试所采用的字符串 facts/二维 edges 形式，在新 prompt 与 Skill 中被明确标记为非法。
- 一个模拟 provider 首次返回该非法形式、第二次返回对象形式时，项目在同一 Intent 上成功达到 `achieved`，不会重新进入 planning。
- 两次尝试的 execution 与日志均可在 snapshot 中观察。
- 严格解析器继续拒绝非 JSON 和非对象 proposals。
- 全部测试通过，README 与实现契约一致。
