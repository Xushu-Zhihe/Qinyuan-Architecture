# NSSCTF Agent Arena 结构化 API 实例

本实例对应 NSSCTF Agent Arena 接口。示例中的平台 token 已脱敏，实际使用时将
`<NSS_AGENT_TOKEN>` 替换为当前有效 token。不要把真实 token 写入文档、日志或提交记录。

## 基础配置

以下配置假设平台 Base URL 填写为：

```text
https://www.nssctf.cn/api
```

因此 endpoint 路径从 `/skill/...` 开始。如果 Base URL 使用
`https://www.nssctf.cn`，则每个路径都需要增加 `/api` 前缀，不能重复添加。

```json
{
  "base_url": "https://www.nssctf.cn/api",
  "auth": {
    "type": "bearer",
    "token": "<NSS_AGENT_TOKEN>",
    "header_name": "Authorization",
    "prefix": "Bearer",
    "parameter_name": "token"
  },
  "default_headers": {
    "Accept": "application/json"
  }
}
```

认证头由顶部的 `auth` 配置自动注入。每个 endpoint 的 `headers` 应保持为空，
不要再次填写 `Authorization`。

## Endpoint 配置

所有 endpoint 的公共传输设置如下：

```json
{
  "timeout_seconds": 30,
  "max_retries": 0,
  "retry_status_codes": [429, 500, 502, 503, 504],
  "query": {},
  "headers": {},
  "form": null
}
```

### 查看当前题目

```json
{
  "method": "GET",
  "path": "/skill/agent/arena/current/",
  "query": {},
  "headers": {},
  "body": null,
  "form": null,
  "timeout_seconds": 30,
  "max_retries": 0,
  "retry_status_codes": [429, 500, 502, 503, 504]
}
```

该 endpoint 只检查当前是否存在进行中的题目，不领取新题。

### 获取题目

```json
{
  "method": "POST",
  "path": "/skill/agent/arena/next/",
  "query": {},
  "headers": {},
  "body": null,
  "form": null,
  "timeout_seconds": 30,
  "max_retries": 0,
  "retry_status_codes": [429, 500, 502, 503, 504]
}
```

### 题目信息

如果 `next` 响应已经包含完整题面，可以关闭此可选 endpoint。保留时使用动态
`external_id`，不要写死某个 attempt ID：

```json
{
  "method": "GET",
  "path": "/skill/agent/arena/attempt/{{external_id}}/",
  "query": {},
  "headers": {},
  "body": null,
  "form": null,
  "timeout_seconds": 30,
  "max_retries": 0,
  "retry_status_codes": [429, 500, 502, 503, 504]
}
```

### 提交结果

```json
{
  "method": "POST",
  "path": "/skill/agent/arena/attempt/{{external_id}}/submit/",
  "query": {},
  "headers": {},
  "body": {
    "flag": "{{submission.value}}"
  },
  "form": null,
  "timeout_seconds": 30,
  "max_retries": 0,
  "retry_status_codes": [429, 500, 502, 503, 504]
}
```

`{{submission.value}}` 会被替换为 solver 生成的 flag。不要填写固定的
`NSSCTF{flag}`，否则平台收到的是字面量字符串。

### 放弃题目

```json
{
  "method": "POST",
  "path": "/skill/agent/arena/attempt/{{external_id}}/abandon/",
  "query": {},
  "headers": {},
  "body": null,
  "form": null,
  "timeout_seconds": 30,
  "max_retries": 0,
  "retry_status_codes": [429, 500, 502, 503, 504]
}
```

## 字段映射

NSSCTF `next` 和 attempt detail 响应中的题目位于 `data.attempt`：

```json
{
  "task_list_path": "data.attempt",
  "external_id_path": "id",
  "name_path": "problem.type_label",
  "goal_path": "problem.content",
  "origin_path": "problem.content",
  "origin_endpoint_path": "problem.container.url.0|problem.container.url",
  "goal_format": "flag_objective",
  "hints_path": "problem.hint",
  "hints_item_path": null,
  "attachments_path": "problem.annex",
  "cursor_path": null,
  "lease_id_path": null
}
```

`problem.content` 为空但附件存在时，后端会生成附件分析目标；`problem.annex` 中的 URL 会被下载到当前自动化会话目录，再把包含全部附件的目录作为只读 `challenge_path` 挂载到每个 solver 沙盒。附件下载不依赖 solver 容器内的网络或 Clash 配置。

如果平台返回的字段名是 `attempt.id`，仍然使用模板变量
`{{external_id}}`；`attempt_id` 不是结构化 API 支持的模板变量。
