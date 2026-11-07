// Fetch + SSE helpers for the local app shell (127.0.0.1:8766).
// /api/*        -> the shell itself (settings, processes, attachments)
// /api/coord/*  -> proxied to the main server (the shell adds the saved session token)

const JSON_HEADERS = { 'content-type': 'application/json' }

const errorText = (data, res) => {
  if (data && typeof data.detail === 'string') return data.detail
  if (data && data.detail) return JSON.stringify(data.detail)
  if (typeof data === 'string' && data) return data
  return `${res.status} ${res.statusText}`
}

const parseBody = async (res) => {
  const text = await res.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

const request = async (path, { method = 'GET', body } = {}) => {
  const init = { method, headers: {} }
  if (body !== undefined) {
    init.headers = JSON_HEADERS
    init.body = JSON.stringify(body)
  }
  const res = await fetch(path, init)
  const data = await parseBody(res)
  if (!res.ok) {
    const err = new Error(errorText(data, res))
    err.status = res.status
    throw err
  }
  return data
}

// The app shell's own API.
export const shell = {
  get: (path) => request(`/api/${path}`),
  post: (path, body = {}) => request(`/api/${path}`, { method: 'POST', body }),
}

// The main server, through the shell's proxy.
export const server = {
  get: (path) => request(`/api/coord/${path}`),
  post: (path, body = {}) => request(`/api/coord/${path}`, { method: 'POST', body }),
  patch: (path, body = {}) => request(`/api/coord/${path}`, { method: 'PATCH', body }),
  del: (path) => request(`/api/coord/${path}`, { method: 'DELETE' }),
}

// Upload one file to the shell; it is read locally and returned as a chat part.
export const attachFile = async (file) => {
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/attach', { method: 'POST', body: form })
  const data = await parseBody(res)
  if (!res.ok) throw new Error(errorText(data, res))
  return data
}

const parseEvent = (payload) => {
  try {
    return JSON.parse(payload)
  } catch {
    return null // keep-alives and malformed lines
  }
}

// Read `data:` lines from an SSE response and call onEvent(json) for each one.
const readEvents = async (res, onEvent) => {
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) return
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop()
    for (const line of lines) {
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (payload === '[DONE]') return
      const event = parseEvent(payload)
      if (event) onEvent(event)
    }
  }
}

// POST /api/chat and stream events: queued, chunks, the final usage event, errors.
export const streamChat = async (body, onEvent, signal) => {
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) {
    const data = await parseBody(res)
    const err = new Error(errorText(data, res))
    err.status = res.status
    throw err
  }
  await readEvents(res, onEvent)
}
