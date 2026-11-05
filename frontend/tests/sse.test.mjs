import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createParser } from '../src/sse.js'
test('split event and split JSON do not drop or duplicate text',()=>{
  const events=[],p=createParser(e=>events.push(e))
  p.push('id: 7\nevent: text\ndata: {"te'); assert.equal(events.length,0)
  p.push('xt":"你好"}\n'); assert.equal(events.length,0)
  p.push('\n: keepalive\n\nid: 9\nevent: done\ndata: {"status":"completed"}\n\n')
  assert.deepEqual(events,[{id:7,kind:'text',data:{text:'你好'}},{id:9,kind:'done',data:{status:'completed'}}])
})
