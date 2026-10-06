<template>
  <div>
    <h1>设置</h1>
    <pre>{{ s }}</pre>

    <h2>全层 / 层页 / 顶条对账</h2>
    <p class="muted">
      默认只报告、不改任何批次；打开「按流水重投影」开关才回写分叉批。
      修复后连跑两次分叉数都应为 0。
    </p>
    <div>
      <label>
        <input type="checkbox" v-model="reproject" />
        按流水重投影（写库修复）
      </label>
    </div>
    <div style="margin:8px 0">
      <button @click="run" :disabled="busy">{{ busy ? '对账中…' : '运行对账' }}</button>
      <span style="margin-left:8px" :class="report && report.open_count === 0 ? 'ok' : 'warn'">
        分叉数：{{ report ? report.open_count : '—' }}
      </span>
    </div>
    <pre v-if="report" class="audit-out">{{ pretty }}</pre>
  </div>
</template>
<script setup>
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
const s = ref('')
const reproject = ref(false)
const busy = ref(false)
const report = ref(null)
const pretty = computed(() => {
  if (!report.value) return ''
  return JSON.stringify({
    status: report.value.status,
    mode: report.value.mode,
    open_count: report.value.open_count,
    aggregate: report.value.aggregate,
    findings: report.value.findings,
    repaired: report.value.repaired,
    inflight_ignored: report.value.inflight_ignored,
  }, null, 2)
})
async function run() {
  if (reproject.value && !window.confirm('将按消费流水重投影并回写分叉批，确定？')) return
  busy.value = true
  try {
    const path = reproject.value ? '/audit/reproject' : '/audit'
    const opts = reproject.value ? { method: 'POST' } : {}
    report.value = await api(path, opts)
  } catch (e) {
    report.value = { status: 'error', open_count: -1, findings: [], error: e.message }
  } finally {
    busy.value = false
  }
}
onMounted(async () => { s.value = JSON.stringify(await api('/settings'), null, 2) })
</script>
<style scoped>
.audit-out { background: rgba(0,0,0,.06); padding: 8px; max-height: 320px; overflow: auto; }
.ok { color: #1a7f37; font-weight: 600; }
.warn { color: #b3541e; font-weight: 600; }
</style>
