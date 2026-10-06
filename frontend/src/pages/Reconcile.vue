<template>
  <div>
    <h1>库存对账</h1>
    <p class="muted">
      同一事实源（业务 lots）上交叉核对三个范围：<b>全层</b> · <b>层页（上/中/下）</b> · <b>顶条</b>。
      默认<b>只报告、不改任何数据</b>；仅当显式打开开关后才以 lots 为准重建只读投影，
      修复过程<b>不会修改或清零业务 lots</b>。
    </p>

    <div class="recon-controls">
      <label class="switch">
        <input type="checkbox" v-model="reproject" />
        允许重投影修复（默认关闭＝只报告）
      </label>
      <button @click="run" :disabled="loading">{{ loading ? '对账中…' : '开始对账' }}</button>
    </div>

    <div v-if="report && report.error" class="recon-status bad">对账失败：{{ report.error }}</div>
    <div v-else-if="report" class="recon-result">
      <div :class="['recon-status', report.divergence_count === 0 ? 'ok' : 'bad']">
        <span v-if="report.divergence_count === 0">✓ 三方一致，分叉数 0（模式：{{ modeText }}）</span>
        <span v-else>✗ 发现 {{ report.divergence_count }} 处分叉（模式：{{ modeText }}）</span>
        <span v-if="report.business_lots_modified" class="danger">
          ｜严重：修复路径触碰了业务 lots（不应发生）
        </span>
      </div>

      <h3>覆盖范围</h3>
      <table class="recon-table">
        <thead>
          <tr><th>范围</th><th>事实（lots 现算）</th><th>投影（页面实显）</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>全层</td>
            <td>{{ report.coverage.full.actual_qty }} / {{ report.coverage.full.actual_lots }} 批</td>
            <td>{{ report.coverage.full.projected_qty }} / {{ report.coverage.full.projected_lots }} 批</td>
            <td :class="qtyClass(report.coverage.full)">
              {{ qtyClass(report.coverage.full) === 'ok-text' ? '一致' : '分叉' }}
            </td>
          </tr>
          <tr v-for="L in layerNames" :key="L">
            <td>{{ layerLabel[L] }} 层页</td>
            <td>{{ report.coverage.layers[L].actual_qty }}</td>
            <td>{{ report.coverage.layers[L].projected_qty }}</td>
            <td :class="qtyClass(report.coverage.layers[L])">
              {{ qtyClass(report.coverage.layers[L]) === 'ok-text' ? '一致' : '分叉' }}
            </td>
          </tr>
          <tr>
            <td>顶条 lot_id</td>
            <td>[{{ report.coverage.bar.actual_lot_ids.join(', ') }}]</td>
            <td>[{{ report.coverage.bar.projected_lot_ids.join(', ') }}]</td>
            <td :class="(arrEq(report.coverage.bar.actual_lot_ids, report.coverage.bar.projected_lot_ids)) ? 'ok-text' : 'bad-text'">
              {{ arrEq(report.coverage.bar.actual_lot_ids, report.coverage.bar.projected_lot_ids) ? '一致' : '分叉' }}
            </td>
          </tr>
        </tbody>
      </table>

      <template v-if="report.divergences.length">
        <h3>分叉明细（含 layer / lot_id）</h3>
        <div v-for="(d, i) in report.divergences" :key="i" class="divergence">
          <div class="divergence-head">
            <span class="badge">{{ d.kind }}</span>
            <b>layer={{ d.layer }}</b>
            <b>lot_id={{ d.lot_id }}</b>
            <span class="muted">{{ d.name }}</span>
          </div>
          <div class="muted">{{ d.detail }}</div>
        </div>
        <p v-if="!reproject" class="muted">
          当前为只报告模式，数据未被改动；勾选「允许重投影修复」后再次对账即可对齐投影。
        </p>
      </template>
    </div>
  </div>
</template>
<script setup>
import { ref, computed } from 'vue'
import { api } from '../api'
const reproject = ref(false)
const loading = ref(false)
const report = ref(null)
const layerNames = ['upper', 'mid', 'lower']
const layerLabel = { upper: '上层', mid: '中层', lower: '下层' }
const modeText = computed(() => report.value
  ? (report.value.mode === 'reprojected' ? '已重投影' : '只报告') : '')
function qtyClass(cell) {
  return Math.abs(cell.actual_qty - cell.projected_qty) < 1e-9 ? 'ok-text' : 'bad-text'
}
function arrEq(a, b) { return a.length === b.length && a.every((v, i) => v === b[i]) }
async function run() {
  loading.value = true
  try {
    report.value = await api('/reconcile', {
      method: 'POST', body: JSON.stringify({ reproject: reproject.value }),
    })
  } catch (e) {
    report.value = { divergence_count: -1, divergences: [], error: e.message }
  } finally {
    loading.value = false
  }
}
</script>
