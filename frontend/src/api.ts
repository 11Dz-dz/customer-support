import { createParser } from './sse.js'
export let csrf = ''
export const setCsrf = (value: string) => { csrf = value }
export class ApiError extends Error { constructor(message: string, public status: number) { super(message) } }
export async function api(path: string, method = 'GET', body?: unknown) {
  const res = await fetch('/api' + path, { method, credentials: 'same-origin', headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRF-Token': csrf } : {}) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
  const data = await res.json()
  if (!res.ok) throw new ApiError(data.message || '请求未成功，请稍后重试', res.status)
  return data
}
export async function watchRun(id: string, signal: AbortSignal, receive: (e: any) => void) {
  let after = 0, ended = false
  for (let attempt = 0; attempt < 4 && !ended; attempt++) {
    if (signal.aborted) return
    try {
      const res = await fetch(`/api/runs/${id}/events?after=${after}`, { credentials: 'same-origin', signal })
      if (!res.ok) throw new ApiError('无法读取本轮结果，请刷新会话', res.status)
      const reader = res.body!.getReader(), decoder = new TextDecoder()
      const parser = createParser((e: any) => {
        if (e.id && e.id <= after) return
        after = Math.max(after, e.id || 0)
        receive(e)
        if (e.kind === 'done' || e.kind === 'error') ended = true
      })
      try {
        while (!ended) {
          const part = await reader.read()
          if (part.done) { parser.push(decoder.decode()); break }
          parser.push(decoder.decode(part.value, { stream: true }))
        }
      } finally { await reader.cancel().catch(() => {}) }
    } catch (err) {
      if (signal.aborted) return
      if (err instanceof ApiError && err.status < 500) throw err
    }
    if (!ended) await new Promise(resolve => setTimeout(resolve, 700 * (attempt + 1)))
  }
  if (!ended && !signal.aborted) throw new Error('连接已断开，可重新打开会话查看结果；消息不会自动重复发送。')
}
