<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from './api'
const props = defineProps<{ admin?: boolean }>()
const rows = ref<any[]>([]), detail = ref<any>(null), status = ref('pending')
const error = ref(''), notice = ref(''), loading = ref(false), saving = ref(false)
const reason = ref(''), confirmed = ref(false), applying = ref(false), note = ref('')
const lookup = ref(''), nextBefore = ref<number | null>(null)
const labels: Record<string,string> = {pending:'待审核', approved:'审核通过', rejected:'审核拒绝', cancelled:'已取消'}
const date = (s:string) => s ? new Date(s).toLocaleString('zh-CN') : '未提供'
let pending: { order: number; reason: string; id: string } | null = null
async function refresh(more = false) {
  loading.value = true; error.value = ''
  try {
    if(props.admin) rows.value = await api(`/admin/refunds?status=${status.value}`)
    else {
      const data = await api(`/orders${more && nextBefore.value ? `?before=${nextBefore.value}` : ''}`)
      rows.value = more ? [...rows.value,...data.orders] : data.orders; nextBefore.value = data.next_before
      if(!more && detail.value) detail.value = await api(`/orders/${detail.value.order.order_id}`)
    }
  } catch(e:any) { error.value = e.message } finally { loading.value = false }
}
async function openOrder(id: number) {
  loading.value = true; error.value = ''; notice.value = ''; detail.value = null; applying.value = false; confirmed.value = false; reason.value = ''; pending = null
  try { detail.value = await api(`/orders/${id}`) }
  catch(e:any) { error.value = e.message } finally { loading.value = false }
}
function findOrder() {
  const value = lookup.value.trim().toUpperCase().replace(/^SO/, '')
  if(!/^\d{1,10}$/.test(value) || +value < 1 || +value > 2147483647) { error.value = '请输入有效的数字订单号'; return }
  void openOrder(+value)
}
async function submit() {
  if(saving.value || !confirmed.value || !reason.value.trim()) return
  const id = detail.value.order.order_id, text = reason.value.trim()
  if(!pending || pending.order !== id || pending.reason !== text) pending = {order:id,reason:text,id:crypto.randomUUID()}
  saving.value = true; error.value = ''
  try {
    const data = await api(`/orders/${id}/refunds`,'POST',{reason:text,client_request_id:pending.id,scope:'whole_order',confirmed:true})
    // Keep the same key on transport failure; history reads never replay the POST.
    pending = null; applying.value = false; confirmed.value = false
    detail.value = await api(`/orders/${id}`); notice.value = data.message
  } catch(e:any) { error.value = e.message } finally {saving.value = false}
}
async function review(decision:string) {
  if(saving.value || !note.value.trim() || !detail.value) return
  saving.value = true; error.value = ''; notice.value = ''
  try {
    detail.value = await api(`/admin/refunds/${detail.value.refund_id}/review`,'POST',{decision,note:note.value.trim()})
    notice.value = '审核结果已保存。未执行资金退款。'; note.value = ''; await refresh()
  } catch(e:any) { error.value = e.message; await refresh() } finally {saving.value = false}
}
onMounted(()=>refresh())
</script>

<template>
  <section class="ticket-page orders-page">
    <div class="page-heading"><div><p class="eyebrow">订单与售后</p><h1>{{admin?'退款申请审核':'我的订单'}}</h1><p class="muted">{{admin?'记录审核结论与说明。审核通过不会触发支付。':'在订单详情确认申请，随时查看审核进度。'}}</p></div><button class="secondary" :disabled="loading || saving" @click="refresh()">↻ 刷新列表</button></div>
    <p v-if="error" class="error" role="alert">{{error}}</p><p v-if="notice" class="success-note" role="status">{{notice}}</p>
    <p class="refund-note">历史样例订单，币种未知。当前仅支持整单申请，退款条件及实际可退金额需人工核对。本系统未接入支付渠道，审核通过不代表退款到账。</p>
    <div v-if="admin" class="tabs"><button v-for="(label,key) in labels" :key="key" :disabled="saving" :class="{active:status===key}" @click="status=key;detail=null;refresh()">{{label}}</button></div>
    <form v-else class="order-lookup" @submit.prevent="findOrder"><input v-model="lookup" placeholder="输入订单号，例如 43659" aria-label="查找订单号"><button class="secondary" :disabled="loading || saving">查找订单</button></form>
    <p v-if="loading" class="muted" role="status">正在读取…</p>
    <template v-if="!admin">
      <div v-if="!detail" class="order-table-wrap"><table class="order-table"><thead><tr><th>订单号</th><th>下单时间</th><th>订单状态</th><th>订单应付总额</th><th></th></tr></thead><tbody><tr v-for="r in rows" :key="r.order_id"><td>{{r.order_number}}</td><td>{{date(r.purchase_at)}}</td><td><span class="order-state"><strong>{{r.display_status}}</strong><small v-if="r.refund_status">履约：{{r.fulfillment_status}}</small></span></td><td>{{r.total_due}}</td><td><button class="secondary" :disabled="loading" @click="openOrder(r.order_id)">查看订单</button></td></tr></tbody></table><p v-if="!rows.length && !loading" class="muted">当前账号没有可查看的订单，请联系管理员核对账号绑定。</p><button v-if="nextBefore" class="secondary" :disabled="loading" @click="refresh(true)">加载更多</button></div>
      <div v-if="detail" class="order-detail"><button class="secondary" :disabled="saving" @click="detail=null;refresh()">← 返回列表</button><div class="page-heading"><div><h2>订单 {{detail.order.order_number}}</h2><p>订单状态：<strong>{{detail.order.display_status}}</strong></p><p v-if="detail.order.refund_status" class="muted">履约状态：{{detail.order.fulfillment_status}}</p></div><button v-if="detail.can_apply && !applying" class="primary" @click="applying=true">申请退款</button></div>
        <dl class="order-facts"><div><dt>下单日期</dt><dd>{{date(detail.order.purchase_at)}}</dd></div><div><dt>订单约定交付日期</dt><dd>{{date(detail.order.due_at)}}</dd></div><div><dt>发货日期</dt><dd>{{date(detail.order.shipped_at)}}</dd></div><div><dt>商品小计</dt><dd>{{detail.order.subtotal}}</dd></div><div><dt>税费 / 运费</dt><dd>{{detail.order.tax_amount}} / {{detail.order.freight}}</dd></div><div><dt>订单应付总额</dt><dd>{{detail.order.total_due}}（币种未知）</dd></div></dl>
        <div class="order-table-wrap"><table class="order-table"><thead><tr><th>商品</th><th>数量</th><th>成交单价</th><th>明细金额</th></tr></thead><tbody><tr v-for="(i,n) in detail.order.items" :key="n"><td>{{i.product_name}}</td><td>{{i.quantity}}</td><td>{{i.unit_price}}</td><td>{{i.line_total}}</td></tr></tbody></table></div>
        <form v-if="applying" class="refund-form" @submit.prevent="submit"><h3>确认整单退款申请</h3><p class="muted">订单应付总额仅供核对，不等于实付或最终可退金额。</p><label>申请原因<textarea v-model="reason" :disabled="saving" required rows="4" maxlength="2000" placeholder="请说明申请原因…"></textarea></label><label class="check-label"><input v-model="confirmed" :disabled="saving" type="checkbox" required>我确认对此订单提交整单申请，提交后等待人工审核。</label><div class="action-row"><button class="primary" :disabled="saving || !confirmed || !reason.trim()">{{saving?'正在提交…':'确认提交申请'}}</button><button type="button" class="secondary" :disabled="saving" @click="applying=false">取消</button></div></form>
        <h3>退款申请记录</h3><button class="secondary" :disabled="saving || loading" @click="openOrder(detail.order.order_id)">刷新订单与申请进度</button><p v-if="!detail.refunds.length" class="muted">尚无退款申请。</p><article v-for="r in detail.refunds" :key="r.refund_id" class="refund-record"><strong>{{r.status_label}}</strong><p>申请编号：{{r.refund_id}} · {{r.scope_label}}</p><p>申请时间：{{date(r.created_at)}}</p><p>原因：{{r.reason}}</p><p v-if="r.review_note">审核说明：{{r.review_note}}（{{date(r.reviewed_at)}}）</p><p class="refund-note">{{r.funds_note}}</p></article>
      </div>
    </template>
    <div v-else class="ticket-grid"><div class="ticket-list"><p v-if="!rows.length && !loading" class="muted">此状态下没有申请。</p><button v-for="r in rows" :key="r.refund_id" class="ticket-card clickable" :disabled="saving" @click="detail=r;note='';notice='' "><span class="badge" :class="r.status">{{r.status_label}}</span><h3>订单 {{r.order_id}}</h3><p class="ticket-id">{{r.refund_id}}</p><p class="resolution-note">{{r.reason}}</p><span class="ticket-link">查看并审核 ↗</span></button></div><aside v-if="detail" class="ticket-detail"><h2>订单 {{detail.order_id}}</h2><p>{{detail.status_label}} · {{detail.scope_label}}</p><p>申请编号：{{detail.refund_id}}</p><p>申请时间：{{date(detail.created_at)}}</p><p>申请原因：{{detail.reason}}</p><p>提交时订单应付总额：{{detail.order_total_snapshot ?? '历史记录未保存'}}（币种未知，非实退金额）</p><p class="refund-note">{{detail.funds_note}}</p><p v-if="detail.review_note">审核说明：{{detail.review_note}}</p><form v-if="detail.status==='pending'" @submit.prevent="review('approved')"><label>审核说明<textarea v-model="note" :disabled="saving" required maxlength="2000" rows="4" placeholder="填写核对情况及审核理由…"></textarea></label><div class="action-row"><button class="primary" :disabled="saving || !note.trim()">审核通过</button><button class="secondary" type="button" :disabled="saving || !note.trim()" @click="review('rejected')">拒绝申请</button></div></form></aside></div>
  </section>
</template>
