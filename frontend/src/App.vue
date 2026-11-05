<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import { api, setCsrf, watchRun } from './api'
import OrdersPanel from './OrdersPanel.vue'
import { upsertInput, appendAnswerChunk, deliveryLabel } from './messageState.js'
const user = ref<any>(null), boot = ref(true), loginBusy = ref(false), error = ref('')
const username = ref(''), password = ref(''), tab = ref('chat')
const authMode = ref<'login'|'register'>('login'), confirmPassword = ref(''), authNotice = ref('')
const conversations = ref<any[]>([]), selected = ref(''), messages = ref<any[]>([]), draft = ref('')
const busy = ref(false), progress = ref(''), messageList = ref<HTMLElement>(), loading = ref(false)
const sending = ref(false), stopping = ref(false), sendMode = ref<'steer'|'followup'>('steer')
const menuOpen = ref(false)
const tickets = ref<any[]>([]), status = ref('pending'), detail = ref<any>(null), note = ref(''), ticketBusy = ref(false)
let controller: AbortController | null = null
let pending: { text: string; id: string; cid: string; mode: 'steer'|'followup' } | null = null
let watchingRun = ''
let navigation = 0
const labels: Record<string,string> = { pending: '待人工处理', in_progress: '人工处理中', resolved: '已完成' }
const reasons: Record<string,string> = { user_requested: '用户请求', uncertain_write: '申请结果核对', persistent_failure: '查询持续失败' }
const formatDate = (value: string) => new Date(value).toLocaleString('zh-CN', { month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit' })
async function scroll() { await nextTick(); messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' }) }
async function refreshList() { conversations.value = await api('/conversations') }
async function login() {
  error.value = ''; loginBusy.value = true
  try { const data = await api('/auth/login', 'POST', { username: username.value, password: password.value }); user.value = data.user; setCsrf(data.csrf_token); password.value = ''; await refreshList() }
  catch(e: any) { error.value = e.message } finally { loginBusy.value = false }
}
function switchAuth() {
  if(loginBusy.value) return
  authMode.value = authMode.value==='login' ? 'register' : 'login'
  password.value = ''; confirmPassword.value = ''; error.value = ''; authNotice.value = ''
}
async function submitAuth() {
  if(loginBusy.value) return
  authNotice.value = ''
  if(authMode.value==='login') { await login(); return }
  error.value = ''
  if(password.value!==confirmPassword.value) { error.value='两次密码不一致'; return }
  loginBusy.value=true
  try {
    await api('/auth/register','POST',{username:username.value,password:password.value,confirm_password:confirmPassword.value})
    password.value=''; confirmPassword.value=''; authMode.value='login'; authNotice.value='注册成功，请使用刚才的账号和密码登录。'
  } catch(e:any) { error.value=e.status===422 ? '请检查账号格式、密码长度和确认密码。' : e.message }
  finally { loginBusy.value=false }
}
async function logout() {
  try { await api('/auth/logout', 'POST'); controller?.abort(); navigation++; user.value = null; setCsrf(''); selected.value = ''; messages.value = []; pending = null; busy.value = false; error.value = ''; tab.value = 'chat' }
  catch(e:any) { error.value = e.message }
}
async function openConversation(cid: string) {
  menuOpen.value = false
  controller?.abort(); watchingRun = ''; stopping.value = false; const epoch = ++navigation
  selected.value = cid; loading.value = true; error.value = ''; busy.value = false; progress.value = ''; pending = null; draft.value = ''
  try {
    const data = await api(`/conversations/${cid}/messages`)
    if (epoch !== navigation) return
    messages.value = data.messages; await scroll()
    if (data.active_run) void stream(data.active_run.run_id, cid, epoch)
  } catch(e:any) { if(epoch===navigation) error.value = e.message } finally { if(epoch===navigation) loading.value = false }
}
async function newConversation() {
  if(loading.value) return
  loading.value = true; error.value = ''
  try { const data = await api('/conversations','POST',{title:'新对话'}); await refreshList(); await openConversation(data.conversation_id); tab.value = 'chat' }
  catch(e:any) { error.value = e.message } finally { loading.value = false }
}
async function stream(rid: string, cid: string, epoch: number) {
  if (watchingRun === rid && busy.value) return
  controller?.abort(); watchingRun = rid
  const local = new AbortController(); controller = local; busy.value = true; progress.value = '正在处理你的请求'
  const seen = new Set<string>()
  try {
    await watchRun(rid,local.signal,e=>{
      if(epoch!==navigation || controller !== local) return
      if(e.kind==='text') { appendAnswerChunk(messages.value,e.data,seen); void scroll() }
      else if(e.kind==='input') { upsertInput(messages.value,e.data); void scroll() }
      else if(e.kind==='progress') progress.value = e.data.message
      else if(e.kind==='error') error.value = e.data.message
    })
    if(epoch===navigation && !local.signal.aborted) {
      const history = await api(`/conversations/${cid}/messages`)
      if(epoch===navigation && controller === local && !local.signal.aborted) { messages.value = history.messages; await refreshList(); await scroll() }
    }
  } catch(e:any) { if(epoch===navigation && !local.signal.aborted) error.value = e.message }
  finally { if(epoch===navigation && controller === local) {busy.value = false; stopping.value = false; watchingRun = ''; progress.value = ''} }
}
async function send(text = draft.value) {
  text = text.trim(); if(!text || sending.value || stopping.value || loading.value) return
  if(!selected.value) { await newConversation(); if(!selected.value) return }
  const cid = selected.value, epoch = navigation
  sending.value = true; error.value = ''
  if(!pending || pending.text!==text || pending.cid!==cid) pending = {text,id:crypto.randomUUID(),cid,mode:sendMode.value}
  try {
    const run = await api(`/conversations/${cid}/messages`,'POST',{message:text,client_request_id:pending.id,mode:pending.mode})
    if(epoch!==navigation) return
    pending = null; if(draft.value.trim()===text) draft.value = ''
    if(run.message_id) upsertInput(messages.value,{message_id:run.message_id,content:text,mode:run.mode,phase:run.phase})
    await scroll()
    if(['pending','running'].includes(run.status)) void stream(run.run_id,cid,epoch)
    else { const history=await api(`/conversations/${cid}/messages`);if(epoch===navigation) messages.value=history.messages }
  } catch(e:any) { if(epoch===navigation) { error.value = e.message; if(!draft.value.trim() || draft.value.trim()===text) draft.value = text } }
  finally { sending.value = false }
}
async function stopRun() {
  const cid=selected.value, epoch=navigation
  if(!cid || stopping.value) return
  stopping.value=true
  try {
    const result=await api(`/conversations/${cid}/stop`,'POST')
    if(epoch===navigation) {progress.value=result.message;if(!result.requested) stopping.value=false}
  } catch(e:any) {if(epoch===navigation) {error.value=e.message;stopping.value=false}}
}
async function showTickets(which = 'tickets') {
  menuOpen.value = false
  tab.value = which; detail.value = null; error.value = ''
  try { tickets.value = await api(which==='admin' ? `/admin/tickets?status=${status.value}` : '/tickets') }
  catch(e:any) { error.value = e.message }
}
async function showDetail(id:string) { error.value = ''; try { detail.value = await api(`/admin/tickets/${id}`); note.value = '' } catch(e:any){error.value=e.message} }
async function act(action:string) {
  ticketBusy.value = true; error.value = ''
  try { const id=detail.value.ticket.ticket_id; await api(`/admin/tickets/${id}/${action}`,'POST',action==='resolve'?{note:note.value}:undefined); await showTickets('admin'); await showDetail(id) }
  catch(e:any){error.value=e.message} finally {ticketBusy.value=false}
}
onMounted(async()=>{
  try { const data=await api('/me'); user.value=data.user;setCsrf(data.csrf_token); await refreshList() }
  catch { /* No session opens the login view. */ } finally {boot.value=false}
})
</script>

<template>
  <div v-if="boot" class="boot">正在打开客服工作台…</div>
  <main v-else-if="!user" class="login-shell">
    <section class="login-story"><div class="wordmark"><span class="brand-icon">r</span> river<span class="wordmark-sub">SUPPORT</span></div><div><p class="eyebrow">每个问题，都有下一步</p><h1>让对话有回应，<br>让服务有着落。</h1><p class="story-copy">商品咨询、订单核实与人工工单，<br>在一个连贯的对话里完成。</p></div><div class="story-bottom"><span class="tiny-dot"></span> 多 Agent 客服工作台 <span>01 / SERVICE</span></div></section>
    <section class="login-form"><div class="login-card">
      <p class="eyebrow">{{authMode==='login'?'欢迎回来':'欢迎加入'}}</p>
      <h2>{{authMode==='login'?'登录客服工作台':'注册客服账号'}}</h2>
      <p class="muted">{{authMode==='login'?'登录账号，继续你的客服对话。':'注册后可进行商品咨询；订单查询需要完成客户身份绑定。'}}</p>
      <p v-if="authNotice" class="success-note" role="status">{{authNotice}}</p>
      <form @submit.prevent="submitAuth">
        <label>账号<input v-model="username" autocomplete="username" :minlength="authMode==='register'?3:1" maxlength="64" :pattern="authMode==='register'?'[A-Za-z0-9_.-]+':undefined" :disabled="loginBusy" required placeholder="输入账号"></label>
        <p v-if="authMode==='register'" class="muted small">3–64位英文字母、数字、点、横线或下划线，大小写不区分。</p>
        <label>密码<input v-model="password" type="password" :autocomplete="authMode==='register'?'new-password':'current-password'" :minlength="authMode==='register'?12:1" maxlength="256" :disabled="loginBusy" required :placeholder="authMode==='register'?'设置12–256位密码':'输入密码'"></label>
        <label v-if="authMode==='register'">确认密码<input v-model="confirmPassword" type="password" autocomplete="new-password" minlength="12" maxlength="256" :disabled="loginBusy" required placeholder="再次输入密码"></label>
        <p v-if="error" class="error" role="alert">{{error}}</p>
        <button class="primary" :disabled="loginBusy">{{loginBusy?(authMode==='register'?'正在注册…':'正在登录…'):(authMode==='register'?'注册账号 →':'登录工作台 →')}}</button>
      </form>
      <button type="button" class="auth-switch" :disabled="loginBusy" @click="switchAuth">{{authMode==='login'?'还没有账号？注册账号':'已有账号？返回登录'}}</button>
      <div class="login-note">演示环境使用历史样例数据。退款功能仅登记申请，不执行资金退款。</div>
    </div></section>
  </main>
  <div v-else class="app-shell">
    <div v-if="menuOpen" class="mobile-scrim" @click="menuOpen=false"></div><aside class="sidebar" :class="{open:menuOpen}"><div class="wordmark"><span class="brand-icon">r</span> river<span class="wordmark-sub">SUPPORT</span></div><button class="new-chat" :disabled="loading || busy" @click="newConversation">＋ 开始新对话</button><div class="nav-label">工作空间</div><button class="nav-item" :class="{active:tab==='chat'}" @click="tab='chat'">◌ 客服对话</button><button class="nav-item" :class="{active:tab==='orders'}" @click="tab='orders';menuOpen=false">▣ 我的订单</button><button v-if="user.role==='admin'" class="nav-item" :class="{active:tab==='refunds'}" @click="tab='refunds';menuOpen=false">▧ 退款审核</button><button class="nav-item" :class="{active:tab==='tickets'}" @click="showTickets()">▤ 我的工单</button><button v-if="user.role==='admin'" class="nav-item" :class="{active:tab==='admin'}" @click="showTickets('admin')">▦ 工单管理</button><div class="nav-label history-label">最近对话 <span>{{conversations.length}}</span></div><div class="conversation-list"><button v-for="c in conversations" :key="c.conversation_id" class="conversation" :class="{selected:selected===c.conversation_id && tab==='chat'}" @click="tab='chat';openConversation(c.conversation_id)"><span>{{c.title}}</span><small>{{formatDate(c.updated_at)}}</small></button><p v-if="!conversations.length" class="empty-small">你的对话会保存在这里</p></div><div class="account"><div class="avatar">{{user.display_name.slice(0,1)}}</div><div><strong>{{user.display_name}}</strong><small>{{user.role==='admin'?'管理员':'客户账号'}}</small></div><button class="logout" aria-label="退出登录" title="退出登录" @click="logout">↗</button></div></aside>
    <main class="workspace"><header class="topbar"><div><button class="menu-toggle" aria-label="展开导航" @click="menuOpen=!menuOpen">☰</button><span class="section-number">{{tab==='chat'?'01':'02'}}</span><strong>{{tab==='chat'?'客服对话':tab==='admin'?'工单管理':tab==='orders'?'我的订单':tab==='refunds'?'退款审核':'我的工单'}}</strong></div><span class="environment">ADVENTUREWORKS · 样例环境</span></header><div v-if="error" class="error global-error" role="alert">{{error}}<button @click="error=''" aria-label="关闭提示">×</button></div>
      <template v-if="tab==='chat'"><div ref="messageList" class="messages"><div v-if="!messages.length && !loading" class="welcome"><div class="welcome-symbol">✳</div><p class="eyebrow">你的服务助手</p><h1>你好，今天想了解什么？</h1><p class="muted">可以一次提出多个问题，我们会逐项处理。</p><div class="suggestions"><button @click="send('推荐一款适合周末骑行的自行车整车。')"><span>01</span><strong>找一款适合的商品</strong><small>描述用途，让我们帮你挑选 ↗</small></button><button @click="send('帮我查一下订单，订单号稍后补充。')"><span>02</span><strong>核实我的订单</strong><small>查询状态、日期及订单明细 ↗</small></button><button @click="send('/human')"><span>03</span><strong>需要人工帮忙</strong><small>创建待处理工单，稍后核对 ↗</small></button></div><p class="welcome-foot">只依据已核实的信息回答，缺少的记录会明确说明。</p></div><div v-if="loading" class="muted loading">正在读取会话…</div><article v-for="(m,i) in messages" :key="m.message_id || i" class="message" :class="m.role"><div class="message-avatar">{{m.role==='user'?user.display_name.slice(0,1):'r'}}</div><div class="message-body"><div class="message-label">{{m.role==='user'?'你':'River 客服'}}<span v-if="m.streaming">正在回复</span><span v-else-if="m.role==='user'" class="delivery">{{deliveryLabel(m)}}</span></div><div class="message-content">{{m.content || (busy?'正在核实相关信息…':'')}}</div></div></article></div><div class="composer-area"><p v-if="busy" class="progress" role="status"><span class="pulse"></span>{{progress || '正在处理，请稍候…'}}</p><div v-if="busy" class="inflight-controls"><label>发送方式 <select v-model="sendMode" :disabled="sending || stopping"><option value="steer">补充当前任务</option><option value="followup">作为下一条问题排队</option></select></label><button type="button" class="secondary" :disabled="stopping" @click="stopRun()">{{stopping?'正在停止…':'停止后续处理'}}</button></div><form class="composer" @submit.prevent="send()"><textarea v-model="draft" :disabled="loading" rows="2" maxlength="4000" placeholder="输入问题，例如：推荐一款自行车，再查一下我的订单…" aria-label="消息内容" @keydown.enter.exact.prevent="send()"></textarea><div class="composer-bottom"><span>{{busy?'处理中可继续补充条件，或选择排队':'Enter 发送 · Shift + Enter 换行'}}</span><button class="send" :disabled="sending || stopping || loading || !draft.trim()" aria-label="发送消息">↑</button></div></form><p class="disclaimer">历史样例数据 · 无实时库存与支付流水 · 人工工单不代表已接通真人客服</p></div></template>
      <OrdersPanel v-else-if="tab==='orders'" :key="'orders'" /><OrdersPanel v-else-if="tab==='refunds'" :key="'refunds'" admin /><section v-else class="ticket-page"><div class="page-heading"><div><p class="eyebrow">服务进度</p><h1>{{tab==='admin'?'让每个问题都有着落':'你的人工处理工单'}}</h1><p class="muted">{{tab==='admin'?'领取工单、记录处理说明，并完成服务闭环。':'工单状态会保存在系统中，可随时回来查看。'}}</p></div><button class="secondary" @click="showTickets(tab)">↻ 刷新</button></div><div v-if="tab==='admin'" class="tabs"><button v-for="(label,key) in labels" :key="key" :class="{active:status===key}" @click="status=key;showTickets('admin')">{{label}}</button></div><div class="ticket-grid"><div class="ticket-list"><div v-if="!tickets.length" class="empty-tickets"><span>▤</span><h3>这里暂时没有工单</h3><p>新的工单会出现在这里。</p></div><button v-for="t in tickets" :key="t.ticket_id" class="ticket-card" :class="{clickable:tab==='admin'}" @click="tab==='admin' && showDetail(t.ticket_id)"><div class="ticket-card-top"><span class="badge" :class="t.status">{{labels[t.status]}}</span><small>{{formatDate(t.created_at)}}</small></div><h3>{{reasons[t.reason] || '人工处理'}}</h3><p class="ticket-id">{{t.ticket_id}}</p><p v-if="t.resolution_note" class="resolution-note">处理说明：{{t.resolution_note}}</p><span v-if="tab==='admin'" class="ticket-link">查看并处理 ↗</span></button></div><aside v-if="detail && tab==='admin'" class="ticket-detail"><button class="close-detail" @click="detail=null">×</button><p class="eyebrow">工单详情</p><h2>{{reasons[detail.ticket.reason]}}</h2><p class="ticket-id">{{detail.ticket.ticket_id}}</p><span class="badge" :class="detail.ticket.status">{{labels[detail.ticket.status]}}</span><div class="timeline"><div v-for="(e,i) in detail.events" :key="i"><strong>{{e.event==='created'?'已创建':e.event==='claim'?'已领取':'已完成'}}</strong><small>{{formatDate(e.created_at)}}</small><p v-if="e.note">{{e.note}}</p></div></div><button v-if="detail.ticket.status==='pending'" class="primary" :disabled="ticketBusy" @click="act('claim')">领取此工单</button><form v-if="detail.ticket.status==='in_progress'" @submit.prevent="act('resolve')"><label>处理说明<textarea v-model="note" maxlength="2000" rows="4" required placeholder="说明核对结果与处理方式…"></textarea></label><button class="primary" :disabled="ticketBusy || !note.trim()">标记处理完成</button><p class="muted small">只有领取此工单的管理员可以完成操作。工单完成不会自动执行退款。</p></form><p v-if="detail.ticket.resolution_note" class="resolution-note">{{detail.ticket.resolution_note}}</p></aside></div></section>
    </main>
  </div>
</template>
