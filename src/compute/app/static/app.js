import { attachFile, server, shell, streamChat } from './api.js'
import { initCode, setCodeModels } from './code.js'
import { chooseFolder } from './folders.js'
import { formatFlops } from './format.js'

// ---------------------------------------------------------------- shared

const $ = (id) => document.getElementById(id)
const PFLOP = 1e15
const STATUS_POLL_MS = 3000
// Reasoning models think before they answer, so give a reply room for both. The server shortens it to what
// your balance and session budget cover.
const MAX_REPLY_TOKENS = 4096
const LIVE_POLL_MS = 2000

const state = {
  settings: null,
  user: null,
  models: [],
  sessionId: null,
  messages: [],
  attachments: [],
  attachNoticeShown: sessionStorage.getItem('attachNoticeShown') === '1',
  queueId: null,
  abort: null,
  loadedUrl: null,   // the server the account, models and leaderboard were last loaded from
  nodeRunning: false,
}

// The message line. `error` marks a failure so the stylesheet can tell it apart from good news.
const say = (text, { error = false } = {}) => {
  const line = $('message')
  line.textContent = text || ''
  line.classList.toggle('error', error)
}

const fail = (err) => say(err && err.message ? err.message : String(err), { error: true })

// Run an async UI action and show its error, if any, in the message line.
const guarded = (fn) => async (event) => {
  if (event && event.type === 'submit') event.preventDefault()
  try {
    await fn(event)
  } catch (err) {
    fail(err)
  }
}

const formatTime = (ts) => {
  if (!ts) return ''
  const ms = typeof ts === 'number' ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(ts)
  return new Date(ms).toLocaleString()
}

const cell = (text) => {
  const td = document.createElement('td')
  td.textContent = text
  return td
}

const row = (values) => {
  const tr = document.createElement('tr')
  tr.append(...values.map(cell))
  return tr
}

const listItem = (text) => {
  const li = document.createElement('li')
  li.textContent = text
  return li
}

const formData = (form) => Object.fromEntries(new FormData(form).entries())

const saveSettings = async (changes) => {
  state.settings = await shell.post('settings', changes)
  return state.settings
}

// --------------------------------------------------------------- connect

const serverState = (srv) => {
  if (!srv.url) return srv.auto_join ? "looking for this network's pool..." : 'not connected'
  const how = srv.auto_join ? "this network's pool" : 'set by hand'
  return `${srv.url} (${srv.reachable ? 'online' : 'unreachable'}, ${how})`
}

const IS_MAC = navigator.userAgent.includes('Mac')

// Shown on the computer that hosts the main server when this network can't see it.
const notAdvertisedWarning = (srv) => [
  "Your main server isn't showing up on this network, so others can't join it automatically.",
  IS_MAC
    ? 'Turn compute on in System Settings > Privacy & Security > Local Network, and allow incoming connections if macOS asks.'
    : 'Allow compute through Windows Defender Firewall on Private networks, and set this network to Private.',
  `Check everyone is on the same Wi-Fi. Meanwhile others can type ${srv.lan_url} under Connect.`,
].join(' ')

// Shown on every computer whose OS blocks the app from the LAN: nothing on the network can work until it's fixed.
const blockedWarning = () => IS_MAC
  ? "macOS is blocking /compute from your local network, so it can't find or reach any other computer. "
    + 'Open System Settings > Privacy & Security > Local Network, turn on python3.13 (/compute runs on it), then quit and reopen /compute. '
    + "If python3.13 isn't listed, install the latest /compute: older builds can't ask macOS for this."
  : "Windows is blocking /compute from your local network. Allow compute through Windows Defender Firewall on Private networks, set this network to Private, then reopen /compute."

const renderServer = (status) => {
  const { server: srv } = status
  $('server-state').textContent = serverState(srv)
  $('server-local-state').textContent = srv.local_running
    ? `Running on this computer. Others can connect to ${srv.lan_url}`
    : 'Not running on this computer.'
  const warning = status.local_network_blocked ? blockedWarning()
    : srv.local_running && srv.advertised === false ? notAdvertisedWarning(srv)   // null: too early to tell
    : ''
  $('server-warning').hidden = !warning
  $('server-warning').textContent = warning
}

// Turns auto-join back on after a typed address.
const findOnLan = async () => {
  say("Looking for this network's pool...")
  const { url } = await shell.post('discover')
  say(`Joined this network's pool at ${url}.`)
  await pollStatus()
}

// A typed address (for a server off this network) stops following the network's pool.
const connectTo = async (event) => {
  const { url } = formData(event.target)
  await saveSettings({ server_url: url, auto_join: false })
  say(`Server set to ${state.settings.server_url}. To go back to this network's pool, click Join this network's pool.`)
  await pollStatus()
}

const startServer = async (event) => {
  const { email, password } = formData(event.target)
  say('Starting the main server...')
  await shell.post('server/start', { email, password })
  event.target.reset()
  say('Main server started.')
  await pollStatus()
}

const stopServer = async () => {
  await shell.post('server/stop')
  say('Main server stopped.')
  await pollStatus()
}

const initConnect = () => {
  $('find-lan').addEventListener('click', guarded(findOnLan))
  $('server-url-form').addEventListener('submit', guarded(connectTo))
  $('server-start-form').addEventListener('submit', guarded(startServer))
  $('server-stop').addEventListener('click', guarded(stopServer))
}

// --------------------------------------------------------------- account

const renderAccount = () => {
  const { user } = state
  $('signed-out').hidden = Boolean(user)
  $('signed-in').hidden = !user
  $('signin-state').textContent = user
    ? `Signed in as ${user.name}${user.is_admin ? ' (admin)' : ''}`
    : 'Not signed in'
  if (!user) return
  $('user-name').textContent = user.name
  $('user-email').textContent = user.email
  $('name-form').elements.name.value = user.name
  $('terms-state').textContent = user.accepted_terms
    ? 'You accepted the current terms.'
    : 'Accept the terms before contributing.'
  $('terms-accept').hidden = user.accepted_terms
}

const loadTerms = async () => {
  const terms = await server.get('terms')
  $('terms-text').textContent = terms.text
  $('terms-accept').dataset.version = terms.version
}

const loadAccount = async () => {
  if (!state.settings.session_token) {
    state.user = null
    renderAccount()
    return
  }
  try {
    state.user = await server.get('auth/me')
  } catch (err) {
    if (err.status === 401) await saveSettings({ session_token: '' })
    state.user = null
  }
  renderAccount()
  if (!state.user) return
  await loadTerms().catch(() => {})
  await loadWallet().catch(() => {})
}

// Save the token in the shell so this computer's node earns for this account.
const signedIn = async ({ user, token }) => {
  await saveSettings({ session_token: token })
  state.user = user
  renderAccount()
  await loadTerms().catch(() => {})
  await loadWallet().catch(() => {})
  say(`Welcome, ${user.name}.`)
}

const login = async (event) => {
  const { email, password } = formData(event.target)
  await signedIn(await server.post('auth/login', { email, password }))
  event.target.reset()
}

const register = async (event) => {
  const { name, email, password } = formData(event.target)
  await signedIn(await server.post('auth/register', { name, email, password }))
  event.target.reset()
}

const logout = async () => {
  await server.post('auth/logout').catch(() => {})
  await saveSettings({ session_token: '' })
  state.user = null
  renderAccount()
  say('Signed out.')
}

const rename = async (event) => {
  const { name } = formData(event.target)
  state.user = await server.patch('auth/me', { name })
  renderAccount()
  say('Name saved.')
}

const acceptTerms = async () => {
  const version = $('terms-accept').dataset.version
  state.user = await server.post('auth/terms', { version })
  renderAccount()
  say('Terms accepted.')
}

const initAccount = () => {
  $('login-form').addEventListener('submit', guarded(login))
  $('register-form').addEventListener('submit', guarded(register))
  $('logout').addEventListener('click', guarded(logout))
  $('name-form').addEventListener('submit', guarded(rename))
  $('terms-accept').addEventListener('click', guarded(acceptTerms))
}

// ------------------------------------------------------------ contribute

const renderSettings = () => {
  const { settings } = state
  const slider = $('memory-form').elements.memory_percent
  if (document.activeElement !== slider) slider.value = settings.memory_percent
  $('memory-value').textContent = slider.value
  const list = $('models-dirs')
  list.replaceChildren(...settings.models_dirs.map(modelsDirItem))
}

const modelsDirItem = (dir) => {
  const li = listItem(`${dir} `)
  const remove = document.createElement('button')
  remove.type = 'button'
  remove.textContent = 'Remove'
  remove.addEventListener('click', guarded(async () => {
    await saveSettings({ models_dirs: state.settings.models_dirs.filter((d) => d !== dir) })
    renderSettings()
    folderSaved(`Removed ${dir}.`)
  }))
  li.append(remove)
  return li
}

// A node scans its model folders when it starts: the shell restarts a running one on its next poll.
const folderSaved = (text) =>
  say(state.nodeRunning ? `${text} Your node restarts with the new folders in a few seconds.` : text)

const renderNode = (node) => {
  state.nodeRunning = node.running
  const detail = node.status.state || node.status.status || ''
  $('node-state').textContent = node.running ? `running (pid ${node.pid}) ${detail}` : 'stopped'
  $('node-start').disabled = node.running
  $('node-stop').disabled = !node.running
}

const addModelsDir = async () => {
  const dir = await chooseFolder({ title: 'Add a model folder' })
  if (!dir) return
  if (state.settings.models_dirs.includes(dir)) return say(`${dir} is already a model folder.`)
  await saveSettings({ models_dirs: [...state.settings.models_dirs, dir] })
  renderSettings()
  folderSaved(`Added ${dir}.`)
}

const saveMemory = async (event) => {
  await saveSettings({ memory_percent: Number(event.target.value) })
  say(`Lending ${state.settings.memory_percent}% of memory (applies the next time you start contributing).`)
}

const startNode = async () => {
  say('Starting the node...')
  renderNode((await shell.post('node/start')).node)
  say('Contributing. The first start downloads llama.cpp, which can take a minute.')
}

const stopNode = async () => {
  renderNode((await shell.post('node/stop')).node)
  say('Stopping: the node finishes its current request, then exits.')
}

const pollLive = async () => {
  if (!state.user) return
  const live = await server.get('wallet/live')
  $('earn-rate').textContent = formatFlops(live.earning_flops_per_s)
  $('earn-total').textContent = formatFlops(live.earned_total)
  $('my-nodes').replaceChildren(...live.nodes.map((n) =>
    listItem(`${n.name}: ${n.online ? 'online' : 'offline'}, ${formatFlops(n.flops_total)}`)))
}

const initContribute = () => {
  const slider = $('memory-form').elements.memory_percent
  slider.addEventListener('input', () => { $('memory-value').textContent = slider.value })
  slider.addEventListener('change', guarded(saveMemory))
  $('models-dir-add').addEventListener('click', guarded(addModelsDir))
  $('node-start').addEventListener('click', guarded(startNode))
  $('node-stop').addEventListener('click', guarded(stopNode))
  setInterval(() => pollLive().catch(() => {}), LIVE_POLL_MS)
}

// ------------------------------------------------------------------ chat

const selectedModel = () => state.models.find((m) => m.id === $('chat-model').value)

const loadModels = async () => {
  const { data } = await server.get('v1/models')
  state.models = data
  const select = $('chat-model')
  const current = select.value
  select.replaceChildren(...data.map((m) => {
    const option = document.createElement('option')
    option.value = m.id
    option.textContent = m.vision ? `${m.id} (images)` : m.id
    return option
  }))
  if (data.some((m) => m.id === current)) select.value = current
  setCodeModels(data)
}

const resetSession = () => {
  state.sessionId = null
  state.messages = []
  $('chat-log').replaceChildren()
  $('session-state').textContent = 'No session (replies are charged to your wallet without a cap).'
}

const renderSession = (session) => {
  $('session-state').textContent =
    `Session ${session.id} on ${session.model}: spent ${formatFlops(session.spent)} of ${formatFlops(session.max_flops)}`
}

const newSession = async (event) => {
  const { model, pflops } = formData(event.target)
  if (!model) throw new Error('Pick a model first.')
  resetSession()
  const session = await server.post('sessions', { model, max_flops: Number(pflops) * PFLOP })
  state.sessionId = session.id
  renderSession(session)
}

const refreshSession = async () => {
  if (!state.sessionId) return
  renderSession(await server.get(`sessions/${state.sessionId}`))
}

const renderAttachments = () => {
  $('attachments').replaceChildren(...state.attachments.map((a, i) => {
    const li = listItem(`${a.name} (${a.kind}, ${Math.ceil(a.size / 1024)} KB) `)
    const remove = document.createElement('button')
    remove.type = 'button'
    remove.textContent = 'Remove'
    remove.addEventListener('click', () => {
      state.attachments.splice(i, 1)
      renderAttachments()
    })
    li.append(remove)
    return li
  }))
}

const addAttachments = async (event) => {
  const files = [...event.target.files]
  event.target.value = ''
  for (const file of files) {
    state.attachments.push(await attachFile(file))
  }
  renderAttachments()
}

const toPart = (a) => (a.kind === 'image'
  ? { type: 'image_url', image_url: { url: a.data_url } }
  : { type: 'text', text: `File: ${a.name}\n\n${a.text}` })

const buildContent = (prompt, attachments) => {
  if (!attachments.length) return prompt
  const parts = attachments.map(toPart)
  return prompt ? [...parts, { type: 'text', text: prompt }] : parts
}

// One-time notice per app session before the first attachment leaves this computer.
const confirmAttachNotice = () => new Promise((resolve) => {
  if (state.attachNoticeShown) return resolve(true)
  const dialog = $('attach-notice')
  dialog.addEventListener('close', () => {
    const ok = dialog.returnValue === 'ok'
    if (ok) {
      state.attachNoticeShown = true
      sessionStorage.setItem('attachNoticeShown', '1')
    }
    resolve(ok)
  }, { once: true })
  dialog.showModal()
})

const chatEntry = (role, text) => {
  const li = document.createElement('li')
  const who = document.createElement('strong')
  who.textContent = `${role}: `
  const body = document.createElement('span')
  body.textContent = text
  const meta = document.createElement('small')
  li.append(who, body, document.createElement('br'), meta)
  $('chat-log').append(li)
  return { body, meta }
}

const showQueue = (queued) => {
  state.queueId = queued ? queued.queue_id : null
  $('queue-state').hidden = !queued
  if (!queued) return
  $('queue-position').textContent = queued.position
  $('queue-eta').textContent = Math.round(queued.eta_s ?? 0)
}

const cancelQueued = async () => {
  if (!state.queueId) return
  await server.del(`queue/${encodeURIComponent(state.queueId)}`)
  showQueue(null)
  say('Request cancelled.')
}

const handleEvent = (reply, event) => {
  if (event.queued) return showQueue(event.queued)
  showQueue(null)
  if (event.error) throw new Error(event.error.message || 'The pool returned an error.')
  const delta = event.choices && event.choices[0] && event.choices[0].delta
  // Reasoning models (Qwen3.5/3.8) stream their thinking first; show it until the answer starts.
  if (delta && delta.reasoning_content) {
    reply.thinking += delta.reasoning_content
    if (!reply.text) reply.view.body.textContent = `(thinking) ${reply.thinking}`
  }
  if (delta && delta.content) {
    reply.text += delta.content
    reply.view.body.textContent = reply.text
  }
  if (event.network) reply.view.meta.textContent = `This reply: ${formatFlops(event.network.flops)}`
}

const validateAttachments = (model) => {
  const hasImage = state.attachments.some((a) => a.kind === 'image')
  if (hasImage && !(model && model.vision)) {
    throw new Error('This model cannot read images. Pick a model marked (images) or remove the image.')
  }
}

const sendMessage = async (event) => {
  const form = event.target
  const prompt = form.elements.prompt.value.trim()
  const model = selectedModel()
  if (!model) throw new Error('Pick a model first (Refresh models).')
  if (!prompt && !state.attachments.length) return
  validateAttachments(model)
  if (state.attachments.length && !(await confirmAttachNotice())) return

  const content = buildContent(prompt, state.attachments)
  const names = state.attachments.map((a) => a.name).join(', ')
  chatEntry('You', names ? `${prompt} [attached: ${names}]` : prompt)
  state.messages.push({ role: 'user', content })
  state.attachments = []
  renderAttachments()
  form.elements.prompt.value = ''

  const reply = { text: '', thinking: '', view: chatEntry('Assistant', '') }
  const body = { model: model.id, messages: state.messages, max_tokens: MAX_REPLY_TOKENS, stream: true }
  if (state.sessionId) body.session_id = state.sessionId
  state.abort = new AbortController()
  setSending(true)
  try {
    await streamChat(body, (ev) => handleEvent(reply, ev), state.abort.signal)
    state.messages.push({ role: 'assistant', content: reply.text })
  } catch (err) {
    state.messages.pop()
    reply.view.meta.textContent = err.name === 'AbortError' ? 'Stopped.' : `Error: ${err.message}`
  } finally {
    showQueue(null)
    setSending(false)
    await refreshSession().catch(() => {})
  }
}

const setSending = (sending) => {
  $('chat-send').disabled = sending
  $('chat-stop').hidden = !sending
}

const stopReply = () => {
  if (state.abort) state.abort.abort()
}

const initChat = () => {
  $('session-form').addEventListener('submit', guarded(newSession))
  $('models-refresh').addEventListener('click', guarded(loadModels))
  $('chat-model').addEventListener('change', resetSession)
  $('chat-form').addEventListener('submit', guarded(sendMessage))
  $('chat-stop').addEventListener('click', stopReply)
  $('queue-cancel').addEventListener('click', guarded(cancelQueued))
  $('attach-input').addEventListener('change', guarded(addAttachments))
}

// ---------------------------------------------------------------- wallet

const loadWallet = async () => {
  const wallet = await server.get('wallet')
  $('wallet-balance').textContent = wallet.unlimited ? 'unlimited (admin)' : formatFlops(wallet.balance)
  $('wallet-earned').textContent = formatFlops(wallet.earned_total)
  $('wallet-spent').textContent = formatFlops(wallet.spent_total)
  $('wallet-reserved').textContent = formatFlops(wallet.reserved)
  const txns = await server.get('wallet/transactions?limit=50')
  $('transactions').replaceChildren(...txns.map((t) =>
    row([formatTime(t.ts), t.kind, formatFlops(t.flops), t.note || ''])))
}

const buy = async (event) => {
  const { pflops } = formData(event.target)
  const { receipt } = await server.post('wallet/checkout', { pflops: Number(pflops) })
  $('receipt').textContent =
    `TEST MODE receipt ${receipt.id}: ${receipt.pflops} PFLOPs, $${receipt.price_usd} (not charged)`
  event.target.reset()
  await loadWallet()
}

const initWallet = () => {
  $('buy-form').addEventListener('submit', guarded(buy))
  $('wallet-refresh').addEventListener('click', guarded(loadWallet))
}

// ----------------------------------------------------------- leaderboard

const loadBoard = async () => {
  const period = $('board-window').querySelector('input:checked').value
  const rows = await server.get(`leaderboard?window=${period}`)
  $('board').replaceChildren(...rows.map((r) => row([r.rank, r.name, formatFlops(r.flops)])))
}

const initBoard = () => {
  $('board-window').addEventListener('change', guarded(loadBoard))
}

// -------------------------------------------------------------- sections

const SECTIONS = ['connect', 'account', 'contribute', 'chat', 'code', 'wallet', 'leaderboard']

const showSection = () => {
  const requested = location.hash.slice(1)
  const id = SECTIONS.includes(requested) ? requested : 'connect'
  if (location.hash !== `#${id}`) history.replaceState(null, '', `#${id}`)
  for (const name of SECTIONS) $(name).hidden = name !== id
  for (const link of document.querySelectorAll('header nav a')) {
    const on = link.getAttribute('href') === `#${id}`
    link.setAttribute('aria-current', on ? 'page' : 'false')
  }
  // The pool's models change as nodes join, leave or add folders: show the current list.
  if ((id === 'chat' || id === 'code') && state.loadedUrl) loadModels().catch(() => {})
}

// ----------------------------------------------------------------- start

// The shell joins this network's pool on each poll, so the server can change under us.
const pollStatus = async () => {
  const status = await shell.get('status')
  state.settings = status.settings
  renderServer(status)
  renderNode(status.node)
  renderSettings()
  const { url, reachable } = status.server
  if (url && reachable && url !== state.loadedUrl) await serverChanged(url)
}

// A new (or newly reachable) server: load the account, models and leaderboard from it.
const serverChanged = async (url) => {
  state.loadedUrl = url
  await loadAccount().catch(fail)
  await loadModels().catch(() => {})
  await loadBoard().catch(() => {})
}

const start = async () => {
  window.addEventListener('hashchange', showSection)
  showSection()
  initConnect()
  initAccount()
  initContribute()
  initChat()
  initWallet()
  initBoard()
  initCode({ say, fail, guarded })
  await pollStatus().catch(fail)
  setInterval(() => pollStatus().catch(() => {}), STATUS_POLL_MS)
}

start()
