<template>
  <section class="page" data-module="crawl_source">
    <header class="page-head">
      <div>
        <h2>采集源管理</h2>
        <p class="page-desc">维护多源采集接入的注册、连接与状态管理，为数据源接入提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记采集源</button>
        <button class="btn" data-testid="validate-pending" type="button" @click="validatePending">校验待接入源</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出采集源管理清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>接入校验</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td :data-testid="`validation-${row.id}`">{{ row._validation_decision ?? '未校验' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`record-${row.id}`" @click="openValidationRecord(row)">
              校验记录
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无采集源管理数据，可先登记采集源</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showValidation" class="modal-backdrop" data-testid="validation-panel">
      <div class="modal">
        <h3>采集源接入校验记录</h3>
        <template v-if="validationRecord">
          <p><strong>记录编号：</strong>{{ validationRecord.record_id }}（数据版本 v{{ validationRecord.version }}）</p>
          <p><strong>源标识：</strong>{{ validationRecord.源标识 }}　<strong>记录状态：</strong>{{ validationRecord.record_status }}</p>
          <p><strong>连通结果：</strong>{{ validationRecord.reachable ? '可达' : '不可达' }}（{{ validationRecord.message }}，延迟 {{ validationRecord.latency }}s）</p>
          <p><strong>接入判定：</strong>{{ validationRecord.decision }}</p>
          <h4>种子状态数据</h4>
          <table class="data-table">
            <thead>
              <tr><th>种子编号</th><th>种子状态</th><th>修复状态</th><th>调度入口</th></tr>
            </thead>
            <tbody>
              <tr v-for="seed in validationRecord.seed_statuses" :key="String(seed.seed_id)">
                <td>{{ seed.种子编号 }}</td>
                <td>{{ seed.status }}</td>
                <td>{{ seed.repair_state || '可用' }}</td>
                <td>{{ seed.schedulable ? '可调度' : '不可调度' }}</td>
              </tr>
              <tr v-if="!validationRecord.seed_statuses?.length">
                <td colspan="4" class="empty-state">该来源暂未绑定种子</td>
              </tr>
            </tbody>
          </table>
        </template>
        <p v-else class="empty-state">该采集源暂无接入校验记录，可先执行提交校验或校验待接入源</p>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="close-validation" @click="showValidation = false">关闭</button>
        </div>
      </div>
    </div>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记采集源</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集源管理记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/crawl_source'
const columns = ["源标识", "源名称", "协议类型", "基础URL", "认证方式", "地区归属", "标签分类", "最近采集时间"]
const actions = ["提交校验", "确认接入", "停用源"]
const statuses = ["草稿", "待校验", "已接入", "已停用"]
const metricNames = ["采集源总数", "待校验源", "已接入源"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const showValidation = ref(false)
const validationRecord = ref<Record<string, any> | null>(null)
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[2]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'crawl_source.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      throw new Error(payload.message || '采集源管理动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集源管理操作失败'
  }
}

async function validatePending() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/validate_pending`, { method: 'POST' })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '待接入源校验失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '待接入源校验失败'
  }
}

async function openValidationRecord(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/validation_record`)
    const payload = await response.json()
    validationRecord.value = payload.record ?? null
    showValidation.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '校验记录读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('采集源列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集源管理列表读取失败'
  }
}

onMounted(reload)
</script>
