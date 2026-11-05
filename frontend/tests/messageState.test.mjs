import { test } from 'node:test'
import assert from 'node:assert/strict'
import { upsertInput,appendAnswerChunk,deliveryLabel } from '../src/messageState.js'

test('an arriving input cannot steal the active assistant text bubble',()=>{
  const messages=[],seen=new Set()
  appendAnswerChunk(messages,{message_id:'answer',text:'first'},seen)
  upsertInput(messages,{message_id:'user2',content:'budget 500',phase:'pending',mode:'steer'})
  appendAnswerChunk(messages,{message_id:'answer',text:' second'},seen)
  assert.equal(messages[0].content,'first second')
  assert.equal(messages[1].content,'budget 500')
  upsertInput(messages,{message_id:'user2',content:'budget 500',phase:'included',mode:'steer'})
  assert.equal(messages.length,2);assert.equal(deliveryLabel(messages[1]),'已纳入处理')
  upsertInput(messages,{message_id:'user2',content:'budget 500',phase:'answered',mode:'steer'})
  upsertInput(messages,{message_id:'user2',content:'budget 500',phase:'pending',mode:'steer'})
  assert.equal(messages[1].phase,'answered')
})

test('follow-up answers stay separate and reconnect replay replaces rather than duplicates',()=>{
  const messages=[],seen=new Set()
  appendAnswerChunk(messages,{message_id:'one',text:'A'},seen)
  appendAnswerChunk(messages,{message_id:'two',text:'B'},seen)
  const replay=new Set()
  appendAnswerChunk(messages,{message_id:'one',text:'A'},replay)
  assert.deepEqual(messages.map(m=>m.content),['A','B'])
  assert.equal(deliveryLabel({phase:'pending',mode:'followup'}),'已排队，等待下一轮')
})
