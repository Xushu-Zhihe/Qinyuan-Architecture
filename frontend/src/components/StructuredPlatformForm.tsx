import type { EndpointInput, StructuredPlatformInput } from '../types'
import { JsonTextarea } from './JsonTextarea'

function EndpointFields({
  title,
  value,
  optional = false,
  disabled,
  onChange,
}: {
  title: string
  value: EndpointInput | null
  optional?: boolean
  disabled: boolean
  onChange: (value: EndpointInput | null) => void
}) {
  const enabled = value !== null
  const endpoint = value ?? { method: 'GET', path: '', query: {}, headers: {}, body: null, form: null, timeout_seconds: 30, max_retries: 0, retry_status_codes: [429, 500, 502, 503, 504] }
  return (
    <fieldset className="endpoint-fields">
      <legend>{title}</legend>
      {optional && <label className="choice-row"><input type="checkbox" checked={enabled} onChange={event => onChange(event.target.checked ? endpoint : null)} disabled={disabled} /><span>启用</span></label>}
      {(!optional || enabled) && <div className="endpoint-row">
        <label className="auto-field"><span>方法</span><select value={endpoint.method} onChange={event => onChange({ ...endpoint, method: event.target.value as EndpointInput['method'] })} disabled={disabled}>{['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].map(method => <option key={method}>{method}</option>)}</select></label>
        <label className="auto-field auto-field--wide"><span>路径</span><input value={endpoint.path} onChange={event => onChange({ ...endpoint, path: event.target.value })} disabled={disabled} placeholder="/tasks/{{external_id}}" /></label>
      </div>}
      {(!optional || enabled) && <div className="endpoint-options">
        <label className="auto-field"><span>超时（秒）</span><input type="number" min={1} max={300} value={endpoint.timeout_seconds} onChange={event => onChange({ ...endpoint, timeout_seconds: Number(event.target.value) })} disabled={disabled} /></label>
        <label className="auto-field"><span>传输重试次数</span><input type="number" min={0} max={10} value={endpoint.max_retries} onChange={event => onChange({ ...endpoint, max_retries: Number(event.target.value) })} disabled={disabled} /></label>
        <label className="auto-field auto-field--wide"><span>重试状态码（逗号分隔）</span><input value={endpoint.retry_status_codes.join(',')} onChange={event => onChange({ ...endpoint, retry_status_codes: event.target.value.split(',').map(item => Number(item.trim())).filter(Number.isFinite) })} disabled={disabled} /></label>
        <JsonTextarea label="查询参数 JSON" value={endpoint.query} emptyValue={{}} objectOnly disabled={disabled} onChange={query => onChange({ ...endpoint, query })} />
        <JsonTextarea label="请求头 JSON" value={endpoint.headers} emptyValue={{}} objectOnly disabled={disabled} onChange={headers => onChange({ ...endpoint, headers })} />
        <JsonTextarea label="请求体 JSON" value={endpoint.body} emptyValue={null} disabled={disabled} onChange={body => onChange({ ...endpoint, body })} />
        <JsonTextarea label="Form JSON" value={endpoint.form} emptyValue={null} objectOnly disabled={disabled} onChange={form => onChange({ ...endpoint, form })} />
      </div>}
    </fieldset>
  )
}

export function StructuredPlatformForm({ value, disabled, onChange }: { value: StructuredPlatformInput; disabled: boolean; onChange: (value: StructuredPlatformInput) => void }) {
  const update = (patch: Partial<StructuredPlatformInput>) => onChange({ ...value, ...patch })
  return (
    <section className="structured-form">
      <div className="auto-fields-grid">
        <label className="auto-field auto-field--wide"><span>平台 Base URL</span><input value={value.base_url} onChange={event => update({ base_url: event.target.value })} disabled={disabled} /></label>
        <label className="auto-field"><span>认证方式</span><select value={value.auth.type} onChange={event => update({ auth: { ...value.auth, type: event.target.value as StructuredPlatformInput['auth']['type'] } })} disabled={disabled}><option value="bearer">Bearer</option><option value="header">Header</option><option value="query">Query</option><option value="cookie">Cookie</option></select></label>
        <label className="auto-field auto-field--wide"><span>平台 token</span><input type="password" autoComplete="off" value={value.auth.token} onChange={event => update({ auth: { ...value.auth, token: event.target.value } })} disabled={disabled} /></label>
        <label className="auto-field"><span>认证 header name</span><input value={value.auth.header_name} onChange={event => update({ auth: { ...value.auth, header_name: event.target.value } })} disabled={disabled} /></label>
        <label className="auto-field"><span>认证 prefix</span><input value={value.auth.prefix} onChange={event => update({ auth: { ...value.auth, prefix: event.target.value } })} disabled={disabled} /></label>
        <label className="auto-field"><span>认证参数名</span><input value={value.auth.parameter_name} onChange={event => update({ auth: { ...value.auth, parameter_name: event.target.value } })} disabled={disabled} /></label>
        <JsonTextarea label="默认 headers JSON" value={value.default_headers} emptyValue={{}} objectOnly className="auto-field--full" disabled={disabled} onChange={default_headers => update({ default_headers })} />
      </div>
      <div className="endpoint-grid">
        <EndpointFields title="查看当前题目" value={value.current_task} optional disabled={disabled} onChange={current_task => update({ current_task })} />
        <EndpointFields title="获取题目" value={value.fetch_task} disabled={disabled} onChange={endpoint => endpoint && update({ fetch_task: endpoint })} />
        <EndpointFields title="题目信息" value={value.task_info} optional disabled={disabled} onChange={task_info => update({ task_info })} />
        <EndpointFields title="提交结果" value={value.submit_result} disabled={disabled} onChange={endpoint => endpoint && update({ submit_result: endpoint })} />
        <EndpointFields title="放弃题目" value={value.skip_task} optional disabled={disabled} onChange={skip_task => update({ skip_task })} />
      </div>
      <details className="auto-advanced">
        <summary>字段映射与结果判断</summary>
        <div className="auto-fields-grid">
          {([['task_list_path', '题目列表'], ['external_id_path', '题目 ID'], ['name_path', '名称'], ['goal_path', 'Goal'], ['origin_path', '题面'], ['origin_endpoint_path', '靶机入口（优先）'], ['hints_path', '提示列表'], ['hints_item_path', '提示项'], ['attachments_path', '附件元数据'], ['cursor_path', '游标'], ['lease_id_path', 'Lease']] as const).map(([field, label]) => <label className="auto-field" key={field}><span>{label} 路径</span><input value={value.mapping[field] ?? ''} onChange={event => update({ mapping: { ...value.mapping, [field]: event.target.value || null } })} disabled={disabled} /></label>)}
          <label className="auto-field"><span>Goal 格式</span><select value={value.mapping.goal_format} onChange={event => update({ mapping: { ...value.mapping, goal_format: event.target.value as StructuredPlatformInput['mapping']['goal_format'] } })} disabled={disabled}><option value="plain">原始题面</option><option value="flag_objective">题目、题面与 Flag 目标</option></select></label>
          <label className="auto-field"><span>接受结果路径</span><input value={value.outcomes.submit.accepted?.path ?? ''} onChange={event => update({ outcomes: { ...value.outcomes, submit: { ...value.outcomes.submit, accepted: event.target.value ? { path: event.target.value, equals: true } : null } } })} disabled={disabled} /></label>
          <label className="auto-field"><span>拒绝结果路径</span><input value={value.outcomes.submit.rejected?.path ?? ''} onChange={event => update({ outcomes: { ...value.outcomes, submit: { ...value.outcomes.submit, rejected: event.target.value ? { path: event.target.value, equals: true } : null } } })} disabled={disabled} /></label>
          <label className="auto-field"><span>题目信息策略</span><select value={value.task_info_mode} onChange={event => update({ task_info_mode: event.target.value as StructuredPlatformInput['task_info_mode'] })} disabled={disabled}><option value="always">始终读取</option><option value="when_incomplete">字段不完整时读取</option></select></label>
          <label className="auto-field"><span>题目信息失败</span><select value={value.task_info_failure_action} onChange={event => update({ task_info_failure_action: event.target.value as StructuredPlatformInput['task_info_failure_action'] })} disabled={disabled}><option value="fail_task">失败当前题目</option><option value="use_fetch_payload">使用取题数据</option><option value="skip_task">请求平台放弃</option></select></label>
          <label className="auto-field"><span>读取题目信息前等待（秒）</span><input type="number" min={0} max={300} step={1} value={value.task_info_delay_seconds} onChange={event => update({ task_info_delay_seconds: Number(event.target.value) })} disabled={disabled} /></label>
          <label className="auto-field"><span>初始游标</span><input value={value.initial_cursor ?? ''} onChange={event => update({ initial_cursor: event.target.value || null })} disabled={disabled} /></label>
        </div>
      </details>
      <EndpointFields title="健康检查（显式 endpoint）" value={value.health_check} optional disabled={disabled} onChange={health_check => update({ health_check })} />
    </section>
  )
}
