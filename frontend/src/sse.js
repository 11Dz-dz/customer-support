// Incremental parser: a network chunk is not necessarily a full SSE event.
export function createParser(onEvent) {
  let buffer = ''
  return {
    push(text) {
      buffer += text
      let i
      while ((i = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, i); buffer = buffer.slice(i + 2)
        const lines = block.split('\n'); let kind = 'message', id = 0; const data = []
        for (const line of lines) {
          if (line.startsWith('event:')) kind = line.slice(6).trim()
          else if (line.startsWith('id:')) id = Number(line.slice(3).trim())
          else if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
        }
        if (data.length) onEvent({ kind, id, data: JSON.parse(data.join('\n')) })
      }
    }
  }
}
