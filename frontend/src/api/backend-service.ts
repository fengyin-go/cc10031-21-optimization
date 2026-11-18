import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 跨模块数据流：获取全平台领域快照 */
export async function loadNodeStats(module?: string): Promise<Record<string, unknown>> {
  const query = module ? `?module=${module}` : ''
  const response = await request(`/api/nodestats${query}`)
  if (!response.ok) throw new Error('节点统计读取失败')
  return await response.json()
}

/** 跨模块数据流：获取指定 topic 的通道消息 */
export async function loadChannelSnapshot(topic: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/channel/${topic}`)
  if (!response.ok) throw new Error('通道快照读取失败')
  return await response.json()
}

/** 跨模块数据流：查询脏数据隔离区 */
export async function loadDirtyVault(source?: string): Promise<Record<string, unknown>> {
  const query = source ? `?source=${source}` : ''
  const response = await request(`/api/dirty-vault${query}`)
  if (!response.ok) throw new Error('脏数据查询失败')
  return await response.json()
}

/** 跨模块数据流：查询采集记录 */
export async function loadCollectRecords(batchRef?: string): Promise<Record<string, unknown>> {
  const query = batchRef ? `?batch_ref=${batchRef}` : ''
  const response = await request(`/api/collect-records${query}`)
  if (!response.ok) throw new Error('采集记录查询失败')
  return await response.json()
}

/** 跨模块数据流：执行RPA全生命周期管道 */
export async function triggerPipeline(lifecycle: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/pipeline/${lifecycle}`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('管道执行失败')
  return await response.json()
}

/** 跨模块数据流：获取失败恢复状态 */
export async function loadRecoveryStats(): Promise<Record<string, unknown>> {
  const response = await request('/api/recovery')
  if (!response.ok) throw new Error('恢复状态查询失败')
  return await response.json()
}

// ---- 异常恢复追踪：重复异常聚合与恢复入口 ----

export type AnomalyGroup = {
  group_no: string
  exec_no: string
  bot_ref: string
  error_type: string
  step: string
  boundary: string
  status: string
  event_count: number
  event_nos: string[]
  recovery_id: number
  recovery_attempt: number
  recovery_max_attempts: number
  recovery_terminal: string
  active_recovery_entry: boolean
  created_at: string
  updated_at: string
  closed_at: string | null
  result_note: string
  events?: EntryRow[]
  recovery_entry?: Record<string, unknown> | null
}

async function recoveryJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await request(`/api/anomaly-recovery${path}`, init)
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(body.detail ?? '异常恢复接口请求失败')
  }
  return (await response.json()) as T
}

/** 恢复入口工作台：只返回仍未关闭的异常组 */
export async function loadActiveGroups(): Promise<{
  items: AnomalyGroup[]
  total: number
  empty: boolean
  empty_text: string
  stats: Record<string, number>
}> {
  return recoveryJson('/groups/active')
}

/** 异常组列表，可按执行编号/状态过滤，含历史 */
export async function loadAnomalyGroups(params: {
  exec_no?: string
  status?: string
}): Promise<{ items: AnomalyGroup[]; total: number; stats: Record<string, number> }> {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, value]) => value),
  ).toString()
  return recoveryJson(`/groups?${query}`)
}

/** 异常组详情：成员事件清单与恢复入口状态同步更新 */
export async function loadAnomalyGroupDetail(groupNo: string): Promise<AnomalyGroup> {
  return recoveryJson(`/groups/${groupNo}`)
}

/** 聚合后的异常事件台账数据集 */
export async function loadAnomalyLedger(execNo?: string): Promise<{ items: AnomalyGroup[]; total: number }> {
  const query = execNo ? `?exec_no=${encodeURIComponent(execNo)}` : ''
  return recoveryJson(`/ledger${query}`)
}

/** 异常事件明细，支持按执行编号查询与历史事件查询 */
export async function loadAnomalyEvents(params: {
  exec_no?: string
  group_no?: string
  history?: boolean
}): Promise<{ items: EntryRow[]; total: number }> {
  const query = new URLSearchParams()
  if (params.exec_no) query.set('exec_no', params.exec_no)
  if (params.group_no) query.set('group_no', params.group_no)
  if (params.history === false) query.set('history', 'false')
  return recoveryJson(`/events?${query.toString()}`)
}

/** 按执行编号核对处理项与异常事件台账 */
export async function reconcileRecovery(execNo: string): Promise<Record<string, unknown>> {
  return recoveryJson(`/reconcile/${encodeURIComponent(execNo)}`)
}

/** 失败上报：同一执行编号的重复失败由后端聚合 */
export async function recordFailure(payload: {
  exec_no: string
  error_type: string
  step: string
  detail?: string
  bot_ref?: string
  boundary?: string
}): Promise<Record<string, unknown>> {
  return recoveryJson('/failures', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/** 恢复入口重试，中断时后端返回明确原因 */
export async function retryAnomalyGroup(groupNo: string): Promise<Record<string, unknown>> {
  return recoveryJson(`/groups/${groupNo}/retry`, { method: 'POST' })
}

/** 确认恢复：按异常组编号写回执行记录，并发两次提交只算一次 */
export async function resolveAnomalyGroup(
  groupNo: string,
  note?: string,
): Promise<Record<string, unknown>> {
  return recoveryJson(`/groups/${groupNo}/resolve`, {
    method: 'POST',
    body: JSON.stringify({ note: note ?? '异常组已恢复，执行记录同步更新' }),
  })
}

/** 忽略异常组 */
export async function ignoreAnomalyGroup(groupNo: string): Promise<Record<string, unknown>> {
  return recoveryJson(`/groups/${groupNo}/ignore`, { method: 'POST' })
}

/** 切换持续故障处理边界：并入当前组 / 关旧开新 */
export async function switchGroupBoundary(
  groupNo: string,
  boundary: string,
): Promise<Record<string, unknown>> {
  return recoveryJson(`/groups/${groupNo}/boundary`, {
    method: 'POST',
    body: JSON.stringify({ boundary }),
  })
}
