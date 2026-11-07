// Code tabs: a pool model reads and edits files in a folder, under Plan / Manual / Auto modes.
// The shell runs each tab's turns (src/compute/app/code/); this module shows them and sends your choices.

import { shell, streamEvents } from './api.js'
import { chooseFolder } from './folders.js'
import { formatFlops } from './format.js'

const $ = (id) => document.getElementById(id)
const PFLOP = 1e15
const RETRY_MS = 1500
const CLOSE_CONFIRM_MS = 3000
const OPEN_DIFF_LINES = 40
const MODE_NAMES = { plan: 'Plan', manual: 'Manual', auto: 'Auto' }
const TOOL_NAMES = {
  list_files: 'List files',
  read_file: 'Read file',
  search: 'Search',
  write_file: 'Write file',
  edit_file: 'Edit file',
  delete_file: 'Delete file',
  run_command: 'Run command',
  propose_plan: 'Propose plan',
}
const STATUS_NAMES = {
  auto: 'done',
  approved: 'approved, done',
  rejected: 'rejected',
  denied: 'not allowed',
  error: 'failed',
}

const api = {
  list: () => shell.get('code/sessions'),
  open: (body) => shell.post('code/sessions', body),
  snapshot: (id, after) => shell.get(`code/sessions/${id}?after=${after}`),
  update: (id, body) => shell.patch(`code/sessions/${id}`, body),
  close: (id) => shell.del(`code/sessions/${id}`),
  send: (id, text) => shell.post(`code/sessions/${id}/messages`, { text }),
  approve: (id, body) => shell.post(`code/sessions/${id}/approval`, body),
  plan: (id, body) => shell.post(`code/sessions/${id}/plan`, body),
  revert: (id, turn) => shell.post(`code/sessions/${id}/revert`, { turn }),
  stop: (id) => shell.post(`code/sessions/${id}/stop`),
}

// id -> { meta, entries, seq, partial, state, stream }
const tabs = new Map()
const ui = { activeId: null, closeArmed: null, say: () => {} }
let noticeShown = sessionStorage.getItem('codeNoticeShown') === '1'

const newTab = (meta) => ({
  meta,
  entries: [],
  seq: 0,
  partial: { text: '', thinking: '' },
  state: { ...meta, pending: null, plan: '', queue: null },
  stream: null,
})

const activeTab = () => tabs.get(ui.activeId) || null
const isActive = (tab) => tab.meta.id === ui.activeId

// ---------------------------------------------------------------- DOM bits

const el = (tag, text, className) => {
  const node = document.createElement(tag)
  if (text !== undefined) node.textContent = text
  if (className) node.className = className
  return node
}

const details = (summary, body, open = false) => {
  const box = el('details')
  box.open = open
  box.append(el('summary', summary), el('pre', body))
  return box
}

const diffLineClass = (line) => {
  if (line.startsWith('+++') || line.startsWith('---')) return 'diff-file'
  if (line.startsWith('@@')) return 'diff-hunk'
  if (line.startsWith('+')) return 'diff-add'
  if (line.startsWith('-')) return 'diff-del'
  return ''
}

const diffView = (diff) => {
  const pre = el('pre', undefined, 'code-diff')
  // One block per line (see .code-diff span), so lines need no trailing newline.
  pre.append(...diff.split('\n').map((line) => el('span', line || ' ', diffLineClass(line))))
  return pre
}

const option = (value) => {
  const opt = el('option', value)
  opt.value = value
  return opt
}

// ---------------------------------------------------------------- the log

const RENDER = {
  user: (e, li) => li.append(el('strong', 'You: '), el('span', e.text)),
  assistant: (e, li) => {
    li.append(el('strong', 'Assistant: '), el('span', e.text || (e.thinking ? '' : '(empty reply)')))
    if (e.thinking) li.append(details('Thinking', e.thinking))
    if (e.flops) li.append(el('br'), el('small', `This step: ${formatFlops(e.flops)}`))
  },
  tool: (e, li) => {
    li.append(el('strong', `${TOOL_NAMES[e.tool] || e.tool}: `), el('code', e.target),
      el('span', ` (${STATUS_NAMES[e.status] || e.status})`, `status-${e.status}`))
    if (e.status !== 'auto' && e.reason) li.append(el('br'), el('small', e.reason))
    if (e.diff) {
      const lines = e.diff.split('\n').length
      const box = el('details')
      box.open = lines <= OPEN_DIFF_LINES
      box.append(el('summary', `Diff (${lines} lines)`), diffView(e.diff))
      li.append(box)
    }
    if (e.output && !e.diff) li.append(details('Result', e.output))
  },
  plan: (e, li) => li.append(el('strong', 'Plan:'), el('pre', e.text)),
  mode: (e, li) => li.append(el('em', `Mode is now ${MODE_NAMES[e.mode] || e.mode}.`)),
  revert: (e, li) => li.append(el('strong', 'Undo: '), el('span', e.text)),
  notice: (e, li) => li.append(el('em', e.text)),
  error: (e, li) => li.append(el('strong', 'Error: '), el('span', e.text)),
}

const entryView = (entry) => {
  const li = el('li', undefined, `entry-${entry.kind}`)
  ;(RENDER[entry.kind] || RENDER.notice)(entry, li)
  return li
}

const scrollLog = () => {
  const log = $('code-log')
  log.scrollTop = log.scrollHeight
}

const partialView = (partial) => {
  const li = el('li', undefined, 'entry-partial')
  li.id = 'code-partial'
  const text = partial.text || (partial.thinking ? `(thinking) ${partial.thinking}` : '')
  li.append(el('strong', 'Assistant (writing): '), el('span', text))
  return li
}

const renderPartial = (tab) => {
  const old = $('code-partial')
  if (old) old.remove()
  if (!tab.partial.text && !tab.partial.thinking) return
  $('code-log').append(partialView(tab.partial))
  scrollLog()
}

const renderLog = (tab) => {
  const log = $('code-log')
  log.dataset.tab = tab.meta.id
  log.replaceChildren(...tab.entries.map(entryView))
  renderPartial(tab)
  scrollLog()
}

const addEntry = (tab, entry) => {
  if (entry.seq <= tab.seq) return
  tab.entries.push(entry)
  tab.seq = entry.seq
  if (!isActive(tab) || $('code-log').dataset.tab !== tab.meta.id) return
  const partial = $('code-partial')
  $('code-log').insertBefore(entryView(entry), partial)
  scrollLog()
}

// ---------------------------------------------------------- tabs and state

const badge = (tab) => {
  const { state, pending, plan } = tab.state
  if (pending) return ' - needs approval'
  if (state !== 'idle') return ' - working'
  if (plan) return ' - plan ready'
  return ''
}

const renderTabs = () => {
  $('code-tabs').replaceChildren(...[...tabs.values()].map((tab) => {
    const button = el('button', `${tab.state.title || tab.meta.title} (${MODE_NAMES[tab.state.mode]})${badge(tab)}`)
    button.type = 'button'
    button.setAttribute('role', 'tab')
    button.setAttribute('aria-selected', String(isActive(tab)))
    button.addEventListener('click', () => select(tab.meta.id).catch((err) => ui.say(err.message)))
    return button
  }))
}

const statusText = (s) => {
  const parts = [`Mode: ${MODE_NAMES[s.mode]}`]
  if (s.state !== 'idle') {
    parts.push(s.queue
      ? `waiting in line: position ${s.queue.position}, about ${Math.round(s.queue.eta_s ?? 0)} s`
      : `${s.activity || 'working'}...`)
  }
  parts.push(s.budget_flops
    ? `spent ${formatFlops(s.spent_flops)} of ${formatFlops(s.budget_flops)}`
    : `spent ${formatFlops(s.spent_flops)} (no budget cap)`)
  return parts.join(' · ')
}

const renderApproval = (tab) => {
  const { pending } = tab.state
  $('code-approval').hidden = !pending
  if (!pending) return
  $('code-approval-what').textContent = `${TOOL_NAMES[pending.tool] || pending.tool}: ${pending.target}`
  $('code-approval-why').textContent = pending.reason
  const diff = $('code-approval-diff')
  diff.hidden = !pending.diff
  diff.replaceChildren(...(pending.diff ? diffView(pending.diff).childNodes : []))
  $('code-approval-form').dataset.id = pending.id
}

const renderPlan = (tab) => {
  const { plan, state } = tab.state
  $('code-plan').hidden = !plan || state !== 'idle'
  $('code-plan-text').textContent = plan || ''
}

// Each turn that changed files can be undone, together with everything after it.
const checkpointItem = (tab, cp) => {
  const li = el('li', `"${cp.label}" changed ${cp.files.join(', ')} `)
  if (cp.reverted) {
    li.append(el('em', '(undone)'))
    return li
  }
  const button = el('button', 'Undo from here')
  button.type = 'button'
  button.disabled = tab.state.state !== 'idle'
  button.addEventListener('click', () => undoFrom(tab, cp.turn).catch((err) => ui.say(err.message)))
  li.append(button)
  return li
}

const renderCheckpoints = (tab) => {
  const checkpoints = tab.state.checkpoints || []
  $('code-checkpoints').hidden = !checkpoints.length
  $('code-checkpoint-list').replaceChildren(...checkpoints.map((cp) => checkpointItem(tab, cp)))
}

const undoFrom = async (tab, turn) => {
  const entry = await api.revert(tab.meta.id, turn)
  await sync(tab)
  ui.say(entry.text)
}

const ensureOption = (select, value) => {
  if (value && ![...select.options].some((o) => o.value === value)) select.append(option(value))
  select.value = value
}

const renderControls = (tab) => {
  const s = tab.state
  $('code-title').textContent = s.title || tab.meta.title
  $('code-root').textContent = tab.meta.root
  $('code-mode').querySelectorAll('input').forEach((input) => { input.checked = input.value === s.mode })
  $('code-run-commands').checked = Boolean(s.run_commands)
  const form = $('code-settings-form')
  ensureOption(form.elements.model, s.model)
  if (document.activeElement !== form.elements.budget_pflops) {
    form.elements.budget_pflops.value = s.budget_flops ? s.budget_flops / PFLOP : 0
  }
  $('code-status').textContent = statusText(s)
  $('code-send').disabled = s.state !== 'idle'
  $('code-stop').hidden = s.state === 'idle'
}

const refresh = (tab) => {
  renderTabs()
  if (!isActive(tab)) return
  renderControls(tab)
  renderApproval(tab)
  renderPlan(tab)
  renderCheckpoints(tab)
}

const renderPanel = () => {
  const tab = activeTab()
  $('code-panel').hidden = !tab
  if (!tab) return
  renderLog(tab)
  refresh(tab)
}

// --------------------------------------------------------------- streaming

const applyState = (tab, state) => {
  tab.state = { ...tab.state, ...state }
  tab.meta = { ...tab.meta, mode: state.mode ?? tab.meta.mode, title: state.title ?? tab.meta.title }
}

const onEvent = (tab, event) => {
  if (event.entry) addEntry(tab, event.entry)
  if (event.partial) {
    tab.partial = event.partial
    if (isActive(tab)) renderPartial(tab)
  }
  if (event.state) {
    applyState(tab, event.state)
    refresh(tab)
  }
}

// Follow a working tab until it is idle again; reconnect if the stream drops mid-turn.
const follow = (tab) => {
  if (tab.stream || !tabs.has(tab.meta.id)) return
  const controller = new AbortController()
  tab.stream = controller
  const path = `/api/code/sessions/${tab.meta.id}/events?after=${tab.seq}`
  streamEvents(path, (event) => onEvent(tab, event), controller.signal)
    .catch((err) => {
      if (err.name !== 'AbortError') console.warn('code stream', err)
    })
    .finally(() => {
      tab.stream = null
      if (controller.signal.aborted || !tabs.has(tab.meta.id)) return
      if (tab.state.state !== 'idle') setTimeout(() => follow(tab), RETRY_MS)
    })
}

// Catch up with entries and state from the shell (after a mode change, on load, on tab switch).
const sync = async (tab) => {
  const snap = await api.snapshot(tab.meta.id, tab.seq)
  snap.entries.forEach((entry) => addEntry(tab, entry))
  tab.partial = snap.partial
  tab.meta = { ...tab.meta, ...snap.session }
  applyState(tab, snap)
  refresh(tab)
  if (snap.state !== 'idle') follow(tab)
}

const select = async (id) => {
  ui.activeId = id
  ui.closeArmed = null
  $('code-close').textContent = 'Close tab'
  renderPanel()
  await sync(tabs.get(id))
}

// ----------------------------------------------------------------- actions

const confirmNotice = () => new Promise((resolve) => {
  if (noticeShown) return resolve(true)
  const dialog = $('code-notice')
  dialog.addEventListener('close', () => {
    const ok = dialog.returnValue === 'ok'
    if (ok) {
      noticeShown = true
      sessionStorage.setItem('codeNoticeShown', '1')
    }
    resolve(ok)
  }, { once: true })
  dialog.showModal()
})

const setRoot = (root) => {
  $('code-new-form').elements.root.value = root
  $('code-root-chosen').textContent = root || 'No folder chosen'
  $('code-root-choose').textContent = root ? 'Change...' : 'Choose folder...'
}

const pickRoot = async () => {
  const start = $('code-new-form').elements.root.value
  const root = await chooseFolder({ title: 'Choose the project folder', start })
  if (root) setRoot(root)
}

const openTab = async (event) => {
  const form = event.target
  const body = Object.fromEntries(new FormData(form).entries())
  if (!body.root) throw new Error('Choose a project folder first.')
  if (!body.model) throw new Error('Pick a model first (Refresh models in the Chat section).')
  if (!(await confirmNotice())) return
  const meta = await api.open({ ...body, budget_pflops: Number(body.budget_pflops || 0) })
  tabs.set(meta.id, newTab(meta))
  await select(meta.id)
  $('code-new').open = false
  ui.say(`Opened ${meta.title} in ${MODE_NAMES[meta.mode]} mode.`)
}

const changeMode = async (event) => {
  const tab = activeTab()
  if (!tab) return
  await api.update(tab.meta.id, { mode: event.target.value })
  await sync(tab)
  ui.say(`Mode is now ${MODE_NAMES[event.target.value]}.`)
}

const changeRunCommands = async (event) => {
  const tab = activeTab()
  if (!tab) return
  await api.update(tab.meta.id, { run_commands: event.target.checked })
  await sync(tab)
  ui.say(event.target.checked ? 'Auto will run commands without asking in this tab.'
    : 'Auto will ask before running commands in this tab.')
}

const updateSettings = async (event) => {
  const tab = activeTab()
  if (!tab) return
  const { model, budget_pflops: pflops } = Object.fromEntries(new FormData(event.target).entries())
  const changes = {}
  if (model && model !== tab.state.model) changes.model = model
  const budget = Number(pflops || 0)
  if (Math.abs(budget * PFLOP - (tab.state.budget_flops || 0)) > 1) changes.budget_pflops = budget
  if (!Object.keys(changes).length) return ui.say('Nothing to update.')
  await api.update(tab.meta.id, changes)
  await sync(tab)
  ui.say('Tab updated.')
}

const send = async (event) => {
  const tab = activeTab()
  const field = event.target.elements.text
  const text = field.value.trim()
  if (!tab || !text) return
  await api.send(tab.meta.id, text)
  field.value = ''
  applyState(tab, { state: 'running', activity: 'thinking', plan: '' })
  refresh(tab)
  follow(tab)
}

const answerApproval = async (event) => {
  const tab = activeTab()
  if (!tab) return
  const approve = event.submitter && event.submitter.value === 'approve'
  const form = event.target
  await api.approve(tab.meta.id, { id: form.dataset.id, approve, note: form.elements.note.value })
  form.reset()
  $('code-approval').hidden = true
}

const answerPlan = async (event) => {
  const tab = activeTab()
  if (!tab) return
  const action = event.submitter ? event.submitter.value : 'revise'
  const form = event.target
  await api.plan(tab.meta.id, { action, note: form.elements.note.value })
  form.reset()
  applyState(tab, { state: 'running', activity: 'thinking', plan: '' })
  await sync(tab)
  follow(tab)
}

const stopTurn = async () => {
  const tab = activeTab()
  if (tab) await api.stop(tab.meta.id)
}

// Closing deletes the transcript, so it takes a second click.
const closeTab = async () => {
  const tab = activeTab()
  const button = $('code-close')
  if (!tab) return
  if (ui.closeArmed !== tab.meta.id) {
    ui.closeArmed = tab.meta.id
    button.textContent = 'Click again to close (deletes this transcript)'
    setTimeout(() => {
      if (ui.closeArmed !== tab.meta.id) return
      ui.closeArmed = null
      button.textContent = 'Close tab'
    }, CLOSE_CONFIRM_MS)
    return
  }
  ui.closeArmed = null
  button.textContent = 'Close tab'
  await api.close(tab.meta.id)
  if (tab.stream) tab.stream.abort()
  tabs.delete(tab.meta.id)
  ui.activeId = tabs.size ? [...tabs.keys()].at(-1) : null
  renderTabs()
  renderPanel()
  if (ui.activeId) await sync(activeTab())
  $('code-new').open = !tabs.size
  ui.say('Tab closed.')
}

// ------------------------------------------------------------------- setup

const fillModels = (select, models) => {
  const current = select.value
  select.replaceChildren(...models.map((m) => option(m.id)))
  if (current) ensureOption(select, current)
}

// Called by app.js whenever the pool's model list is refreshed.
export const setCodeModels = (models) => {
  fillModels($('code-model'), models)
  fillModels($('code-tab-model'), models)
  const tab = activeTab()
  if (tab) ensureOption($('code-tab-model'), tab.state.model)
}

const loadTabs = async () => {
  const { sessions } = await api.list()
  sessions.forEach((meta) => {
    if (!tabs.has(meta.id)) tabs.set(meta.id, newTab(meta))
  })
  if (!ui.activeId && tabs.size) ui.activeId = [...tabs.keys()][0]
  renderPanel()
  await Promise.all([...tabs.values()].map(sync))
  $('code-new').open = !tabs.size
  renderPanel()
}

export const initCode = ({ say, guarded }) => {
  ui.say = say
  $('code-new-form').addEventListener('submit', guarded(openTab))
  $('code-root-choose').addEventListener('click', guarded(pickRoot))
  $('code-mode').addEventListener('change', guarded(changeMode))
  $('code-run-commands').addEventListener('change', guarded(changeRunCommands))
  $('code-settings-form').addEventListener('submit', guarded(updateSettings))
  $('code-form').addEventListener('submit', guarded(send))
  $('code-approval-form').addEventListener('submit', guarded(answerApproval))
  $('code-plan-form').addEventListener('submit', guarded(answerPlan))
  $('code-stop').addEventListener('click', guarded(stopTurn))
  $('code-close').addEventListener('click', guarded(closeTab))
  $('code-form').elements.text.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault()
      $('code-form').requestSubmit()
    }
  })
  loadTabs().catch((err) => say(err.message))
}
