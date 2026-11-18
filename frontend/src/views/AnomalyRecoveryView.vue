<template>
  <section class="page" data-module="anomaly_recovery">
    <header class="page-head">
      <div>
        <h2>异常恢复追踪</h2>
        <p class="page-desc">
          同一执行对象的重复异常自动聚合为异常组，恢复入口只处理仍未关闭的异常组，
          重试五次不再产生五条待办
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-groups" type="button" @click="reloadAll">
          刷新待办
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">活动异常组（恢复入口）</span>
        <strong class="stat-value" data-testid="stat-active">{{ stats.active_groups ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">组内事件总数</span>
        <strong class="stat-value">{{ stats.event_total ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已恢复组</span>
        <strong class="stat-value">{{ stats.recovered_groups ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已关闭/已忽略组</span>
        <strong class="stat-value">
          {{ (stats.closed_groups ?? 0) + (stats.ignored_groups ?? 0) }}
        </strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reloadAll">
      <label class="filter-item">
        <span>执行编号</span>
        <input
          v-model="execFilter"
          data-testid="filter-exec"
          placeholder="按执行编号查询异常组"
        />
      </label>
      <label class="filter-item">
        <span>组状态</span>
        <select v-model="statusFilter" data-testid="filter-status">
          <option value="">全部（含历史）</option>
          <option value="活动">活动</option>
          <option value="已恢复">已恢复</option>
          <option value="已关闭">已关闭</option>
          <option value="已忽略">已忽略</option>
        </select>
      </label>
      <button class="btn" data-testid="query-groups" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table" data-testid="group-table">
      <thead>
        <tr>
          <th>异常组编号</th>
          <th>执行编号</th>
          <th>关联流程</th>
          <th>异常类型</th>
          <th>成员事件数</th>
          <th>处理边界</th>
          <th>组状态</th>
          <th>恢复入口</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="group in groups"
          :key="group.group_no"
          :data-testid="`group-row-${group.group_no}`"
          :data-status="group.status"
          @click="openDetail(group.group_no)"
        >
          <td>{{ group.group_no }}</td>
          <td>{{ group.exec_no }}</td>
          <td>{{ group.bot_ref }}</td>
          <td>{{ group.error_type }}</td>
          <td data-testid="event-count">{{ group.event_count }}</td>
          <td>{{ group.boundary }}</td>
          <td>{{ group.status }}</td>
          <td>
            <template v-if="group.active_recovery_entry">
              重试 {{ group.recovery_attempt }}/{{ group.recovery_max_attempts }}
            </template>
            <template v-else>入口已{{ group.recovery_terminal || group.status }}</template>
          </td>
          <td class="row-actions" @click.stop>
            <button
              v-if="group.status === '活动'"
              class="link"
              data-testid="action-retry"
              type="button"
              @click="onRetry(group.group_no)"
            >
              重试
            </button>
            <button
              v-if="group.status === '活动'"
              class="link"
              data-testid="action-resolve"
              type="button"
              @click="onResolve(group.group_no)"
            >
              确认恢复
            </button>
            <button
              v-if="group.status === '活动'"
              class="link"
              data-testid="action-ignore"
              type="button"
              @click="onIgnore(group.group_no)"
            >
              忽略
            </button>
            <button
              v-if="group.status === '活动'"
              class="link"
              type="button"
              @click="onSwitchBoundary(group)"
            >
              切换边界
            </button>
            <button class="link" type="button" @click="openDetail(group.group_no)">
              详情
            </button>
          </td>
        </tr>
        <tr v-if="!groups.length">
          <td colspan="9" class="empty-state" data-testid="group-empty">
            {{ emptyText || '暂无失败：所有异常组均已关闭，可在历史台账中查询' }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="selected" class="modal-backdrop" data-testid="detail-modal">
      <div class="modal">
        <h3>异常组详情 {{ selected.group_no }}</h3>
        <dl class="dag-detail">
          <dt>执行编号</dt><dd>{{ selected.exec_no }}</dd>
          <dt>关联流程</dt><dd>{{ selected.bot_ref }}</dd>
          <dt>异常类型/步骤</dt><dd>{{ selected.error_type }} / {{ selected.step }}</dd>
          <dt>组状态</dt><dd>{{ selected.status }}</dd>
          <dt>处理边界</dt><dd>{{ selected.boundary }}</dd>
          <dt>成员事件数</dt><dd>{{ selected.event_count }}</dd>
          <dt>恢复入口</dt>
          <dd>
            <template v-if="selected.recovery_entry">
              重试 {{ selected.recovery_attempt }}/{{ selected.recovery_max_attempts }}，
              终态：{{ selected.recovery_terminal || '未关闭' }}
              <span v-if="selected.result_note">｜{{ selected.result_note }}</span>
            </template>
            <template v-else>无活动恢复入口</template>
          </dd>
          <dt>创建/关闭时间</dt>
          <dd>{{ selected.created_at }} ／ {{ selected.closed_at ?? '—' }}</dd>
        </dl>

        <h4>成员事件与处理结果清单</h4>
        <table class="data-table" data-testid="event-table">
          <thead>
            <tr>
              <th>事件编号</th>
              <th>执行编号</th>
              <th>异常详情</th>
              <th>发生时间</th>
              <th>事件状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="event in selected.events ?? []" :key="String(event.id)">
              <td>{{ event['事件编号'] }}</td>
              <td>{{ event['执行编号'] }}</td>
              <td>{{ event['异常详情'] }}</td>
              <td>{{ event['发生时间'] }}</td>
              <td>{{ event['status'] }}</td>
            </tr>
          </tbody>
        </table>

        <div class="page-actions">
          <button
            v-if="selected.status === '活动'"
            class="btn primary"
            data-testid="detail-retry"
            type="button"
            @click="onRetry(selected.group_no)"
          >
            恢复入口重试
          </button>
          <button
            v-if="selected.status === '活动'"
            class="btn primary"
            data-testid="detail-resolve"
            type="button"
            @click="onResolve(selected.group_no)"
          >
            确认恢复并写回
          </button>
          <button class="btn" type="button" @click="selected = null">关闭</button>
        </div>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ groups.length }} 个异常组，恢复入口仅指向未关闭异常组</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-text">{{ errorMessage }}</span>
      <span v-else-if="infoMessage" class="info-text" data-testid="info-text">{{ infoMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  ignoreAnomalyGroup,
  loadAnomalyGroupDetail,
  loadAnomalyGroups,
  resolveAnomalyGroup,
  retryAnomalyGroup,
  switchGroupBoundary,
  type AnomalyGroup,
} from '@/api/backend-service'

const groups = ref<AnomalyGroup[]>([])
const stats = ref<Record<string, number>>({})
const selected = ref<AnomalyGroup | null>(null)
const execFilter = ref('')
const statusFilter = ref('')
const errorMessage = ref('')
const infoMessage = ref('')
const emptyText = ref('')

async function reloadAll() {
  errorMessage.value = ''
  try {
    const payload = await loadAnomalyGroups({
      exec_no: execFilter.value.trim() || undefined,
      status: statusFilter.value || undefined,
    })
    groups.value = payload.items
    stats.value = payload.stats
    emptyText.value =
      payload.total === 0 && !execFilter.value && !statusFilter.value
        ? '暂无失败：所有异常组均已关闭，可在历史台账中查询'
        : ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '异常组读取失败'
  }
}

function resetFilters() {
  execFilter.value = ''
  statusFilter.value = ''
  void reloadAll()
}

async function openDetail(groupNo: string) {
  try {
    selected.value = await loadAnomalyGroupDetail(groupNo)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '详情读取失败'
  }
}

async function onRetry(groupNo: string) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const result = await retryAnomalyGroup(groupNo)
    infoMessage.value = String((result as { message?: string }).message ?? '重试已提交')
  } catch (error) {
    // 重试中断等 409 场景：展示后端给出的明确原因
    errorMessage.value = error instanceof Error ? error.message : '重试失败'
  }
  await reloadAll()
  if (selected.value?.group_no === groupNo) {
    await openDetail(groupNo)
  }
}

async function onResolve(groupNo: string) {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const result = await resolveAnomalyGroup(groupNo)
    infoMessage.value = String((result as { message?: string }).message ?? '异常组已恢复')
    if (selected.value?.group_no === groupNo) {
      selected.value = null
    }
  } catch (error) {
    // 并发第二次提交会被后端幂等拒绝，这里展示“只算一次”的原因
    errorMessage.value = error instanceof Error ? error.message : '确认恢复失败'
  }
  await reloadAll()
}

async function onIgnore(groupNo: string) {
  errorMessage.value = ''
  try {
    await ignoreAnomalyGroup(groupNo)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '忽略失败'
  }
  await reloadAll()
}

async function onSwitchBoundary(group: AnomalyGroup) {
  errorMessage.value = ''
  const next = group.boundary === '并入当前组' ? '关旧开新' : '并入当前组'
  try {
    const result = await switchGroupBoundary(group.group_no, next)
    infoMessage.value = String((result as { message?: string }).message ?? '边界已切换')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '边界切换失败'
  }
  await reloadAll()
}

onMounted(reloadAll)
</script>
