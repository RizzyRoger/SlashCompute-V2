// /compute admin page. Served by the main server at /admin; auth is the compute_session cookie.

const $ = (id) => document.getElementById(id)
const PFLOP = 1e15

// ---------------------------------------------------------------- fetch

const errorText = (data, res) => {
  if (data && typeof data.detail === 'string') return data.detail
  if (data && data.detail) return JSON.stringify(data.detail)
  return `${res.status} ${res.statusText}`
}

const request = async (path, { method = 'GET', body, raw } = {}) => {
  const init = { method, credentials: 'same-origin', headers: {} }
  if (raw !== undefined) {
    init.body = raw
    init.headers['content-type'] = 'application/octet-stream'
  } else if (body !== undefined) {
    init.body = JSON.stringify(body)
    init.headers['content-type'] = 'application/json'
  }
  const res = await fetch(path, init)
  const text = await res.text()
  let data = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }
  if (!res.ok) {
    const err = new Error(errorText(data, res))
    err.status = res.status
    throw err
  }
  return data
}

const api = {
  get: (path) => request(path),
  post: (path, body = {}) => request(path, { method: 'POST', body }),
  del: (path) => request(path, { method: 'DELETE' }),
}

// ------------------------------------------------------------- helpers

// The message line. `error` marks a failure so the stylesheet can tell it apart from good news.
const say = (text, { error = false } = {}) => {
  const line = $('message')
  line.textContent = text || ''
  line.classList.toggle('error', error)
}

const fail = (err) => say(err.message, { error: true })

const guarded = (fn) => async (event) => {
  if (event && event.type === 'submit') event.preventDefault()
  try {
    await fn(event)
  } catch (err) {
    fail(err)
  }
}

const formatFlops = (n) => {
  if (n === null || n === undefined) return '-'
  const units = [[1e18, 'EFLOPs'], [1e15, 'PFLOPs'], [1e12, 'TFLOPs'], [1e9, 'GFLOPs']]
  const match = units.find(([size]) => Math.abs(n) >= size)
  return match ? `${(n / match[0]).toFixed(3)} ${match[1]}` : `${Math.round(n)} FLOPs`
}

const formatTime = (ts) => {
  if (!ts) return ''
  const ms = typeof ts === 'number' ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(ts)
  return new Date(ms).toLocaleString()
}

const yesNo = (value) => (value ? 'yes' : 'no')

const cell = (content) => {
  const td = document.createElement('td')
  if (content instanceof Node) td.append(content)
  else td.textContent = content ?? ''
  return td
}

const row = (values) => {
  const tr = document.createElement('tr')
  tr.append(...values.map(cell))
  return tr
}

const button = (label, onClick) => {
  const b = document.createElement('button')
  b.type = 'button'
  b.textContent = label
  b.addEventListener('click', guarded(onClick))
  return b
}

const buttons = (...items) => {
  const span = document.createElement('span')
  span.append(...items)
  return span
}

// ---------------------------------------------------------------- auth

const showPanel = (user) => {
  $('login').hidden = Boolean(user)
  $('panel').hidden = !user
  $('who').textContent = user ? `Signed in as ${user.email}` : ''
}

const checkSession = async () => {
  try {
    const user = await api.get('/auth/me')
    if (!user.is_admin) throw new Error('This account is not the admin.')
    showPanel(user)
    await refresh()
  } catch (err) {
    showPanel(null)
    if (err.status !== 401) fail(err)
  }
}

const login = async (event) => {
  const { email, password } = Object.fromEntries(new FormData(event.target).entries())
  await api.post('/auth/login', { email, password })
  event.target.reset()
  say('')
  await checkSession()
}

const logout = async () => {
  await api.post('/auth/logout')
  showPanel(null)
}

// ------------------------------------------------------------ overview

const loadOverview = async () => {
  const o = await api.get('/admin/api/overview')
  const items = [
    ['Users', o.users],
    ['Nodes online', o.nodes_online],
    ['FLOPs earned (all users)', formatFlops(o.flops_earned_total)],
    ['FLOPs purchased (test mode)', formatFlops(o.flops_purchased_total)],
    ['Queue length', o.queue_length],
  ]
  $('overview-list').replaceChildren(...items.flatMap(([term, value]) => {
    const dt = document.createElement('dt')
    dt.textContent = term
    const dd = document.createElement('dd')
    dd.textContent = value
    return [dt, dd]
  }))
}

// --------------------------------------------------------------- users

const credit = async (user) => {
  const pflops = Number(prompt(`Credit how many PFLOPs to ${user.email}?`, '1'))
  if (!pflops) return
  const note = prompt('Note (optional)', '') || ''
  await api.post(`/admin/api/users/${user.id}/credit`, { flops: pflops * PFLOP, note })
  say(`Credited ${pflops} PFLOPs to ${user.email}.`)
  await loadUsers()
}

const setBanned = async (user, banned) => {
  await api.post(`/admin/api/users/${user.id}/ban`, { banned })
  await loadUsers()
}

const setFlagged = async (user, flagged) => {
  const reason = flagged ? prompt(`Why flag ${user.email}?`, '') : ''
  if (reason === null) return
  await api.post(`/admin/api/users/${user.id}/flag`, { flagged, reason })
  await loadUsers()
}

const userRow = (u) => row([
  u.email,
  u.name,
  formatFlops(u.balance),
  formatFlops(u.earned_total),
  formatFlops(u.spent_total),
  u.flagged ? `yes: ${u.flag_reason || ''}` : 'no',
  yesNo(u.banned),
  formatTime(u.created),
  buttons(
    button('Credit FLOPs', () => credit(u)),
    u.banned ? button('Unban', () => setBanned(u, false)) : button('Ban', () => setBanned(u, true)),
    u.flagged ? button('Unflag', () => setFlagged(u, false)) : button('Flag', () => setFlagged(u, true)),
  ),
])

const loadUsers = async () => {
  const users = await api.get('/admin/api/users')
  $('users-body').replaceChildren(...users.map(userRow))
}

// ---------------------------------------------------------------- pool

const stopPipeline = async (id) => {
  await api.post(`/inference/pipelines/${encodeURIComponent(id)}/stop`)
  await loadPool()
}

const removeModel = async (id) => {
  if (!confirm(`Remove model ${id}?`)) return
  await api.del(`/inference/models/${encodeURIComponent(id)}`)
  await loadPool()
}

const nodeRow = (n) => row([
  n.name, n.chip, yesNo(n.online), n.committed_gb, n.in_use_gb, yesNo(n.can_head), yesNo(n.draining), n.build,
])

const pipelineRow = (p) => row([
  p.id,
  p.model,
  p.state,
  p.live_tok_s ?? p.est_tok_s ?? '',
  (p.members || []).map((m) => `${m.node} (${m.role} ${m.layer_start}-${m.layer_end})`).join(', '),
  p.broken_reason || p.explanation || '',
  button('Stop', () => stopPipeline(p.id)),
])

const modelRow = (m) => row([
  m.id, m.status, m.size_gb, m.arch, (m.heads || []).join(', '), yesNo(m.uploaded),
  button('Remove', () => removeModel(m.id)),
])

const loadPool = async () => {
  const status = await api.get('/inference/status')
  $('nodes-body').replaceChildren(...(status.nodes || []).map(nodeRow))
  $('pipelines-body').replaceChildren(...(status.pipelines || []).map(pipelineRow))
  $('models-body').replaceChildren(...(status.models || []).map(modelRow))
}

// --------------------------------------------------------------- queue

const cancelQueued = async (id) => {
  await api.del(`/queue/${encodeURIComponent(id)}`)
  await loadQueue()
}

const loadQueue = async () => {
  const { models } = await api.get('/queue')
  const rows = Object.entries(models || {}).flatMap(([model, entries]) => entries.map((q) => row([
    model, q.queue_id, q.position, Math.round(q.eta_s ?? 0), button('Cancel', () => cancelQueued(q.queue_id)),
  ])))
  $('queue-body').replaceChildren(...rows)
}

// -------------------------------------------------------------- upload

const upload = async (event) => {
  const form = event.target
  const file = form.elements.file.files[0]
  if (!file) return
  const name = form.elements.name.value.trim() || file.name
  $('upload-progress').hidden = false
  say(`Uploading ${name}...`)
  try {
    await request(`/inference/models/upload?name=${encodeURIComponent(name)}`, { method: 'POST', raw: file })
  } finally {
    $('upload-progress').hidden = true
  }
  form.reset()
  say(`Uploaded ${name}.`)
  await loadPool()
}

// --------------------------------------------------------------- start

const refresh = async () => {
  const results = await Promise.allSettled([loadOverview(), loadUsers(), loadPool(), loadQueue()])
  const failed = results.find((r) => r.status === 'rejected')
  if (failed) fail(failed.reason)
}

$('login-form').addEventListener('submit', guarded(login))
$('logout').addEventListener('click', guarded(logout))
$('refresh').addEventListener('click', guarded(refresh))
$('upload-form').addEventListener('submit', guarded(upload))
checkSession()
