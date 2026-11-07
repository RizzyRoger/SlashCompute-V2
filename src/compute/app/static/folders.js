// Folder picker: choose a folder by clicking, instead of typing a path.
// In the app window it opens the system dialog (Finder / File Explorer). In a plain browser it falls back
// to #folder-picker, which browses the sub-folders the shell lists (GET /api/folders).

import { shell } from './api.js'

const $ = (id) => document.getElementById(id)
const LAST_KEY = 'folderPickerLast'

// What the open picker shows. `token` drops answers that arrive after the user has moved on.
const view = { path: '', parent: null, hidden: false, token: 0 }

const lastFolder = () => {
  try {
    return localStorage.getItem(LAST_KEY) || ''
  } catch {
    return ''
  }
}

const remember = (path) => {
  try {
    localStorage.setItem(LAST_KEY, path)
  } catch {
    // private window or blocked storage: the picker just starts at home next time
  }
}

const folderQuery = (path) => {
  const params = new URLSearchParams()
  if (path) params.set('path', path)
  if (view.hidden) params.set('hidden', 'true')
  return `folders?${params}`
}

const button = (text, onClick, className) => {
  const node = document.createElement('button')
  node.type = 'button'
  node.textContent = text
  if (className) node.className = className
  node.addEventListener('click', onClick)
  return node
}

const folderItem = (folder) => {
  const open = button('', () => go(folder.path), 'folder-item')
  const icon = document.createElement('span')
  icon.className = 'folder-icon'
  icon.setAttribute('aria-hidden', 'true')
  open.append(icon, folder.name)
  const li = document.createElement('li')
  li.append(open)
  return li
}

const placeButton = (place, current) => {
  const node = button(place.name, () => go(place.path))
  node.title = place.path
  if (place.path === current) node.setAttribute('aria-current', 'location')
  return node
}

const separator = () => {
  const node = document.createElement('span')
  node.textContent = '›'
  node.setAttribute('aria-hidden', 'true')
  return node
}

// `/ › Users › me`, each part a button. The last one is the folder on show.
const crumbTrail = (crumbs) => crumbs.flatMap((crumb, index) => {
  const node = button(crumb.name, () => go(crumb.path))
  if (index === crumbs.length - 1) node.setAttribute('aria-current', 'location')
  return index ? [separator(), node] : [node]
})

const countText = (data) => {
  const n = data.folders.length
  const count = n ? `${n} folder${n === 1 ? '' : 's'} inside.` : 'No folders inside.'
  return data.truncated ? `${count} Showing the first ${n}.` : count
}

const render = (data) => {
  view.path = data.path
  view.parent = data.parent
  $('folder-places').replaceChildren(...data.places.map((p) => placeButton(p, data.path)))
  $('folder-crumbs').replaceChildren(...crumbTrail(data.crumbs))
  $('folder-up').disabled = !data.parent
  $('folder-list').replaceChildren(...data.folders.map(folderItem))
  $('folder-list').scrollTop = 0
  $('folder-status').textContent = countText(data)
  const choose = $('folder-choose')
  choose.textContent = `Choose "${data.name}"`
  choose.disabled = false
  const first = $('folder-list').querySelector('button')
  ;(first || choose).focus()
}

const SLOW_MS = 1500

// Show one folder. If the folder to start in is gone, fall back to home.
const go = async (path) => {
  const token = ++view.token
  $('folder-status').textContent = 'Loading...'
  // macOS waits for a permission prompt the first time an app opens Desktop, Documents, Downloads...
  setTimeout(() => {
    if (token !== view.token || $('folder-status').textContent !== 'Loading...') return
    $('folder-status').textContent = 'Still opening. If your computer asks whether /compute may open this folder, answer that prompt.'
  }, SLOW_MS)
  try {
    const data = await shell.get(folderQuery(path))
    if (token === view.token) render(data)
  } catch (err) {
    if (token !== view.token) return
    if (!view.path && path) return go('')
    $('folder-status').textContent = err.message
  }
}

let wired = false

const wire = () => {
  if (wired) return
  wired = true
  $('folder-up').addEventListener('click', () => view.parent && go(view.parent))
  $('folder-hidden').addEventListener('change', (event) => {
    view.hidden = event.target.checked
    go(view.path)
  })
}

// The in-app browser, for when the UI runs in a plain web browser (no system dialog to call).
const browseForFolder = ({ title, start }) => new Promise((resolve) => {
  wire()
  const dialog = $('folder-picker')
  $('folder-picker-title').textContent = title
  $('folder-choose').disabled = true
  $('folder-list').replaceChildren()
  view.path = ''
  view.parent = null
  dialog.returnValue = ''
  dialog.addEventListener('close', () => {
    view.token += 1
    const chosen = dialog.returnValue === 'choose' && view.path ? view.path : null
    if (chosen) remember(view.parent || chosen)
    resolve(chosen)
  }, { once: true })
  dialog.showModal()
  go(start || lastFolder())
})

// In the app window, pywebview hands the page the system folder picker (src/compute/app/main.py).
const systemChooser = () => window.pywebview?.api?.choose_folder

// Ask the user for a folder: Finder on macOS, File Explorer on Windows, the in-app browser elsewhere.
// Resolves to the chosen folder's full path, or null if the user cancels.
export const chooseFolder = async ({ title = 'Choose a folder', start = '' } = {}) => {
  const system = systemChooser()
  if (system) return (await system(start)) || null
  return browseForFolder({ title, start })
}
