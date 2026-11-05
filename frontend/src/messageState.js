/** @param {any[]} messages @param {any} input */
export function upsertInput(messages, input) {
  const old = messages.find(m => m.message_id === input.message_id)
  const value = { ...input, role: 'user' }
  if (old && ['answered','cancelled','interrupted'].includes(old.phase) &&
      ['pending','included'].includes(value.phase)) value.phase = old.phase
  if (old) Object.assign(old, value)
  else messages.push(value)
}

/** @param {any[]} messages @param {any} data @param {Set<string>} seen */
export function appendAnswerChunk(messages, data, seen) {
  const id = data.message_id
  let answer = messages.find(m => m.message_id === id)
  if (!answer) {
    answer = { message_id: id, role: 'assistant', content: '', streaming: true }
    messages.push(answer)
    answer = messages[messages.length - 1]
  }
  if (!seen.has(id)) { answer.content = ''; seen.add(id) }
  answer.streaming = true
  answer.content += data.text
}

/** @param {any} message */
export function deliveryLabel(message) {
  if (message.phase === 'pending') return message.mode === 'followup' ? '已排队，等待下一轮' : '已接收，等待纳入处理'
  if (message.phase === 'included') return '已纳入处理'
  if (message.phase === 'cancelled') return '已取消待处理消息'
  if (message.phase === 'interrupted') return '本次处理已中断，请核实后重新发送'
  return ''
}
