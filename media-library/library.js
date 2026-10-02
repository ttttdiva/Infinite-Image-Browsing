import { justifiedLayout, visibleRects } from './layout.js';
import { ratingFromKey, shortcutTargetIds } from './rating-shortcuts.js';

const $ = (id) => document.getElementById(id);
const base = document.querySelector('meta[name="iib-base"]').content;
const apiBase = base + '/library-api';
const embedded = window.parent !== window;
const SORT_OPTIONS = Object.freeze({
  newest: { field: 'mtime', direction: 'desc', label: '更新日時（降順・新しい順）' },
  oldest: { field: 'mtime', direction: 'asc', label: '更新日時（昇順・古い順）' },
  name: { field: 'name', direction: 'asc', label: '名前（昇順）' },
  name_desc: { field: 'name', direction: 'desc', label: '名前（降順）' },
});
const DEFAULT_SORT = 'newest';
const FOLDER_BOOKMARKS_STORAGE_KEY = 'iib:library:folder-bookmarks';
const SELECTED_FOLDER_STORAGE_KEY = 'iib:library:selected-folder';

function canonicalFolderPath(value) {
  if (typeof value !== 'string') return '';
  const raw = value.trim();
  if (!raw) return '';
  const windowsPath = /^[A-Za-z]:(?:[\\/]|$)/.test(raw) || /^\\\\/.test(raw) || /^\/\//.test(raw);
  let path = raw;
  if (!windowsPath) {
    if (path.length > 1) path = path.replace(/\/+$/, '');
    return path;
  }
  path = path.replace(/\//g, '\\');
  if (!path) return '';
  const unc = path.startsWith('\\\\');
  path = path.replace(/\\{2,}/g, '\\');
  if (unc && !path.startsWith('\\\\')) path = '\\' + path;
  if (!/^[A-Za-z]:\\$/.test(path)) path = path.replace(/\\+$/, '');
  return path;
}
function isWindowsFolderPath(path) { return /^[A-Za-z]:(?:\\|$)/.test(path) || path.startsWith('\\\\'); }
function normalizeFolderPath(value) {
  const path = canonicalFolderPath(value);
  return isWindowsFolderPath(path) ? path.toLowerCase() : path;
}
function folderBookmarkLabel(path) {
  const canonical = canonicalFolderPath(path);
  const parts = canonical.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] || canonical;
}
function folderDriveBadge(path) {
  const match = /^([A-Za-z]):(?:\\|$)/.exec(canonicalFolderPath(path));
  return match ? match[1].toUpperCase() + ':' : '';
}
function decorateFolderButton(target, label, path) {
  const name = String(label || folderBookmarkLabel(path));
  const fullPath = canonicalFolderPath(path) || String(path || '').trim();
  target.replaceChildren(el('span', 'folder-label', name));
  const drive = folderDriveBadge(fullPath);
  if (drive) target.append(el('span', 'folder-drive', drive));
  target.title = fullPath;
  target.setAttribute('aria-label', name + '（' + fullPath + '）');
}
function folderOptionLabel(label, path) {
  const name = String(label || folderBookmarkLabel(path)), drive = folderDriveBadge(path);
  return drive ? name + ' [' + drive + ']' : name;
}
function loadFolderBookmarks() {
  let parsed;
  try {
    const raw = localStorage.getItem(FOLDER_BOOKMARKS_STORAGE_KEY);
    if (!raw) return [];
    parsed = JSON.parse(raw);
  } catch { return []; }
  const entries = Array.isArray(parsed) ? parsed : (Array.isArray(parsed?.bookmarks) ? parsed.bookmarks : []);
  const seen = new Set(), bookmarks = [];
  for (const entry of entries) {
    const candidate = typeof entry === 'string' ? entry : entry?.path;
    const path = canonicalFolderPath(candidate), key = normalizeFolderPath(path);
    if (!key || seen.has(key)) continue;
    seen.add(key); bookmarks.push(path);
  }
  return bookmarks;
}
function persistFolderBookmarks() {
  try { localStorage.setItem(FOLDER_BOOKMARKS_STORAGE_KEY, JSON.stringify(state.folderBookmarks)); } catch { /* 保存できない環境でも画面操作は継続する。 */ }
}
function folderRootsContaining(path, roots = state.roots) {
  const key = normalizeFolderPath(path);
  if (!key) return [];
  return roots.filter(root => {
    const rootKey = normalizeFolderPath(root?.path);
    if (!rootKey) return false;
    const separator = isWindowsFolderPath(rootKey) ? '\\' : '/';
    const prefix = rootKey.endsWith(separator) ? rootKey : rootKey + separator;
    return key === rootKey || key.startsWith(prefix);
  });
}
function persistSelectedFolder(path) {
  try {
    const canonical = canonicalFolderPath(path);
    if (canonical) localStorage.setItem(SELECTED_FOLDER_STORAGE_KEY, canonical);
    else localStorage.removeItem(SELECTED_FOLDER_STORAGE_KEY);
  } catch { /* 保存できない環境でもフォルダ移動は継続する。 */ }
}
function restoreSelectedFolder(roots) {
  let stored = '';
  try { stored = localStorage.getItem(SELECTED_FOLDER_STORAGE_KEY) || ''; } catch { return ''; }
  const path = canonicalFolderPath(stored);
  if (path && folderRootsContaining(path, roots).length) return path;
  if (stored) persistSelectedFolder('');
  return '';
}
function isFolderBookmarked(path) {
  const key = normalizeFolderPath(path);
  return Boolean(key) && state.folderBookmarks.some(bookmark => normalizeFolderPath(bookmark) === key);
}
function removeFolderBookmark(path, announceChange = true) {
  const key = normalizeFolderPath(path);
  if (!key) return false;
  const next = state.folderBookmarks.filter(bookmark => normalizeFolderPath(bookmark) !== key);
  if (next.length === state.folderBookmarks.length) return false;
  state.folderBookmarks = next; persistFolderBookmarks(); renderFolders();
  if (announceChange) announce('ブックマークを解除しました');
  return true;
}
function toggleFolderBookmark(path) {
  const canonical = canonicalFolderPath(path), key = normalizeFolderPath(canonical);
  if (!key) return;
  if (isFolderBookmarked(canonical)) { removeFolderBookmark(canonical); return; }
  state.folderBookmarks.push(canonical); persistFolderBookmarks(); renderFolders(); announce('ブックマークに追加しました');
}
function folderBookmarkAvailable(path) {
  return folderRootsContaining(path).some(root => root.available !== false);
}
function folderContextMenuOpen() { return !$('folder-context-menu').hidden; }
function closeFolderContextMenu(restoreFocus = false) {
  const menu = $('folder-context-menu');
  if (!menu || menu.hidden) return false;
  menu.hidden = true;
  const target = closeFolderContextMenu.target;
  closeFolderContextMenu.target = null;
  if (restoreFocus && target?.isConnected) target.focus();
  return true;
}
function openFolderContextMenu(path, anchor, x, y, mode = 'auto') {
  const menu = $('folder-context-menu');
  if (!menu) return;
  const remove = mode === 'remove' || (mode === 'auto' && isFolderBookmarked(path));
  closeFolderContextMenu();
  closeFolderContextMenu.target = anchor?.querySelector?.('.folder-item') || anchor;
  menu.replaceChildren();
  const action = button(remove ? 'ブックマークを解除' : 'ブックマークに追加', () => {
    closeFolderContextMenu();
    if (remove) removeFolderBookmark(path); else toggleFolderBookmark(path);
  });
  action.setAttribute('role', 'menuitem');
  menu.append(action);
  menu.hidden = false;
  menu.style.left = Math.max(8, Number(x) || 0) + 'px';
  menu.style.top = Math.max(8, Number(y) || 0) + 'px';
  const bounds = menu.getBoundingClientRect(), margin = 8;
  const maxLeft = Math.max(margin, innerWidth - bounds.width - margin);
  const maxTop = Math.max(margin, innerHeight - bounds.height - margin);
  menu.style.left = Math.min(Math.max(margin, Number(x) || 0), maxLeft) + 'px';
  menu.style.top = Math.min(Math.max(margin, Number(y) || 0), maxTop) + 'px';
  action.focus();
}
function bindFolderContextMenu(row, target, path, mode = 'auto') {
  row.dataset.folderPath = path;
  target.dataset.folderPath = path;
  row.addEventListener('contextmenu', event => {
    event.preventDefault(); event.stopPropagation();
    openFolderContextMenu(path, row, event.clientX, event.clientY, mode);
  });
  target.addEventListener('keydown', event => {
    if (event.key !== 'ContextMenu' && !(event.key === 'F10' && event.shiftKey)) return;
    event.preventDefault(); event.stopPropagation();
    const bounds = row.getBoundingClientRect();
    openFolderContextMenu(path, row, bounds.left + Math.min(bounds.width, 180), bounds.bottom, mode);
  });
}
function normalizeSort(value) { return Object.prototype.hasOwnProperty.call(SORT_OPTIONS, value) ? value : DEFAULT_SORT; }
function sortFromControls() {
  const field = $('sort-field')?.value === 'name' ? 'name' : 'mtime';
  const direction = $('sort-direction')?.value === 'asc' ? 'asc' : 'desc';
  if (field === 'name') return direction === 'asc' ? 'name' : 'name_desc';
  return direction === 'asc' ? 'oldest' : 'newest';
}
function persistSort() { localStorage.setItem('iib:library:sort', normalizeSort(state.filters.sort)); }
function syncSortControls() {
  const sort = SORT_OPTIONS[normalizeSort(state.filters.sort)];
  const field = $('sort-field'), direction = $('sort-direction');
  if (field) { field.value = sort.field; field.title = sort.label; }
  if (direction) {
    const descending = direction.querySelector('option[value="desc"]');
    const ascending = direction.querySelector('option[value="asc"]');
    if (descending) descending.textContent = sort.field === 'name' ? '降順（Z→A）' : '降順（新しい順）';
    if (ascending) ascending.textContent = sort.field === 'name' ? '昇順（A→Z）' : '昇順（古い順）';
    direction.value = sort.direction;
    direction.title = sort.field === 'name'
      ? (sort.direction === 'desc' ? '名前の降順（Z→A）' : '名前の昇順（A→Z）')
      : (sort.direction === 'desc' ? '更新日時の降順（新しい順）' : '更新日時の昇順（古い順）');
  }
}
const state = {
  items: [], cursor: '', total: 0, loading: false, hasMore: false,
  filters: { q: '', folder: '', source: '', model: '', lora: '', tags: [], kind: 'all', sort: normalizeSort(localStorage.getItem('iib:library:sort')) },
  folderOpen: new Set(), folderChildren: new Map(), roots: [], folderBookmarks: loadFolderBookmarks(), facets: [], selected: new Set(), activeId: null, focusId: null, detail: null,
  rects: [], height: 0, width: 0, request: 0, detailRequest: 0, index: {},
  target: Number(localStorage.getItem('iib:library:size')) || 320,
  sidebar: localStorage.getItem('iib:library:sidebar') !== 'false',
  ratingMax: localStorage.getItem('iib:library:rating-max') === '10' ? 10 : 5,
  viewerId: null, viewerScale: 1, viewerRequest: 0, tagEditorIds: [],
};
const mounted = new Map();
let scrollFrame = 0, searchTimer, statusTimer, previousFocus, viewerPreviousFocus;
const icons = {
  folders: ['M3 4h18v16H3z', 'M8 4v16'], search: ['M16 16l5 5', 'M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0'],
  filter: ['M3 4h18l-7 8v7l-4 2v-9z'], refresh: ['M20 7v5h-5', 'M4 17v-5h5', 'M5 8a8 8 0 0 1 13-3l2 3', 'M19 16a8 8 0 0 1-13 3l-2-3'],
  size: ['M3 4h18v16H3z', 'M3 16l5-6 4 4 3-3 6 6'], copy: ['M8 8h13v13H8z', 'M16 3H3v13'],
  download: ['M12 3v12', 'M7 10l5 5 5-5', 'M4 16v5h16v-5'], expand: ['M8 3H3v5', 'M16 3h5v5', 'M3 16v5h5', 'M21 16v5h-5'],
};
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.6'); svg.setAttribute('aria-hidden', 'true');
  for (const d of icons[name] || []) { const path = document.createElementNS(svg.namespaceURI, 'path'); path.setAttribute('d', d); svg.append(path); }
  return svg;
}
function el(tag, className = '', content = '') {
  const result = document.createElement(tag); if (className) result.className = className;
  if (content !== '') result.textContent = String(content); return result;
}
function button(label, action, className = '') {
  const result = el('button', className, label); result.type = 'button'; result.addEventListener('click', action); return result;
}
function announce(message) {
  $('toast').textContent = message; $('toast').hidden = false;
  clearTimeout(announce.timer); announce.timer = setTimeout(() => { $('toast').hidden = true; }, 3500);
}
async function api(path, body) {
  const response = await fetch(apiBase + path, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!response.ok) {
    if (response.status === 401 && embedded) window.parent.postMessage({ source: 'iib-media-library', action: 'reconnect' }, location.origin);
    let message = response.status === 401 ? '接続が切れました。画面を再読み込みしてください。' : '処理できませんでした。もう一度お試しください。';
    try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; } catch { /* 通信エラー本文を表示しない。 */ }
    throw new Error(message);
  }
  return response.json();
}
async function uploadDroppedFile(file, dest) {
  const url = apiBase + '/upload-image?dest=' + encodeURIComponent(dest) + '&filename=' + encodeURIComponent(file.name);
  const response = await fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/octet-stream' }, body: file });
  if (!response.ok) {
    let message = '画像を追加できませんでした';
    try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; } catch {}
    throw new Error(message);
  }
  return response.json();
}
function fileUrl(id, type = 'thumbnail', size = 768) { return `${apiBase}/items/${id}/${type}${type === 'thumbnail' ? `?size=${size}` : ''}`; }
function filterCount() { return ['folder','source','model','lora'].filter(k => state.filters[k]).length + state.filters.tags.length + Number(state.filters.kind !== 'all'); }
function updateChrome() {
  const count = filterCount(); $('filter-count').hidden = !count; $('filter-count').textContent = String(count);
  $('count').textContent = state.total.toLocaleString() + ' 件';
  syncSortControls();
  const location = $('folder-location');
  if (location) {
    const path = canonicalFolderPath(state.filters.folder);
    const label = path ? '表示フォルダ: ' + path : 'すべての参照フォルダ';
    location.textContent = label;
    location.title = path || label;
    location.setAttribute('aria-label', label);
  }
  $('selection-bar').hidden = !state.selected.size;
  $('selection-count').textContent = state.selected.size + ' 件選択';
  $('toggle-folders').setAttribute('aria-expanded', String(state.sidebar));
  $('folders').hidden = !state.sidebar;
  $('library').classList.toggle('has-sidebar', state.sidebar);
  $('all-media').classList.toggle('active', !state.filters.folder && !state.filters.tags.length);
  for (const [id, element] of mounted) element.classList.toggle('selected', state.selected.has(id));
}

function captureAnchor() {
  const top = $('gallery-scroll').scrollTop;
  const rect = state.rects.find(r => r.y + r.height > top);
  return rect ? { id: rect.id, delta: top - rect.y } : null;
}
function layout(anchor = null) {
  const host = $('gallery-scroll');
  const width = Math.max(1, host.clientWidth - 16);
  const result = justifiedLayout(state.items, width, state.target, 6);
  state.width = width; state.rects = result.rects; state.height = result.height;
  $('gallery-canvas').style.height = Math.ceil(result.height) + 'px';
  if (anchor) { const rect = state.rects.find(r => r.id === anchor.id); if (rect) host.scrollTop = Math.max(0, rect.y + anchor.delta); }
  renderWindow();
}
function renderWindow() {
  const host = $('gallery-scroll');
  const rects = visibleRects(state.rects, host.scrollTop, host.clientHeight);
  const visibleIds = new Set(rects.map(r => r.id));
  for (const [id, element] of mounted) { if (!visibleIds.has(id)) { element.remove(); mounted.delete(id); } }
  for (const rect of rects) {
    let tile = mounted.get(rect.id);
    if (!tile) {
      tile = el('button', 'image-tile'); tile.type = 'button'; tile.dataset.imageId = String(rect.id); tile.draggable = true;
      tile.setAttribute('aria-label', '画像 ' + (rect.index + 1) + ' の詳細を開く');
      tile.addEventListener('dragstart', event => {
        const ids = state.selected.has(rect.id) && state.selected.size ? [...state.selected] : [rect.id];
        state.focusId = rect.id;
        event.dataTransfer?.setData('application/x-iib-media-ids', JSON.stringify(ids.slice(0, 500)));
        if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
      });
      const image = el('img'); image.alt = ''; image.loading = 'lazy'; image.decoding = 'async';
      image.width = state.items[rect.index].width; image.height = state.items[rect.index].height;
      image.src = fileUrl(rect.id);
      image.addEventListener('error', () => { image.hidden = true; tile.append(el('span', 'thumbnail-error', '読み込めません')); }, { once: true });
      const like = el('span', 'tile-like', '♥'); like.title = 'Like';
      tile.append(image, like, el('span', 'tile-rating'));
      tile.addEventListener('focus', () => { state.focusId = rect.id; renderWindow(); });
      tile.addEventListener('click', event => {
        state.focusId = rect.id;
        if (event.ctrlKey || event.metaKey || event.shiftKey) {
          if (event.shiftKey && state.lastClicked !== undefined) {
            const a = state.items.findIndex(x => x.id === state.lastClicked), b = state.items.findIndex(x => x.id === rect.id);
            if (a >= 0 && b >= 0) for (const item of state.items.slice(Math.min(a,b), Math.max(a,b) + 1)) state.selected.add(item.id);
          } else if (state.selected.has(rect.id)) state.selected.delete(rect.id); else state.selected.add(rect.id);
          state.lastClicked = rect.id; updateChrome(); return;
        }
        state.lastClicked = rect.id; openInspector(rect.id);
      });
      tile.addEventListener('dblclick', () => { state.focusId = rect.id; openViewer(rect.id, true); });
      mounted.set(rect.id, tile); $('gallery-canvas').append(tile);
    }
    tile.style.transform = `translate(${rect.x}px,${rect.y}px)`;
    tile.style.width = rect.width + 'px'; tile.style.height = rect.height + 'px';
    tile.classList.toggle('selected', state.selected.has(rect.id));
    tile.classList.toggle('active', rect.id === state.activeId && !$('inspector').hidden);
    tile.classList.toggle('focused', rect.id === state.focusId);
    const item = state.items[rect.index];
    const rating = item?.rating || 0;
    const badge = tile.querySelector('.tile-rating');
    badge.textContent = rating ? '★ ' + rating : '';
    badge.hidden = !rating;
    tile.querySelector('.tile-like').hidden = !item?.liked;
  }
  host.dataset.renderedCount = String(mounted.size); host.dataset.loadedCount = String(state.items.length);
  if (state.hasMore && !state.loading && host.scrollTop + host.clientHeight >= state.height - 1400) void loadNext();
}

function focusGalleryId(id, focusDom = true) {
  if (id == null || !state.items.some(item => item.id === id)) return false;
  state.focusId = id;
  const rect = state.rects.find(item => item.id === id);
  const host = $('gallery-scroll');
  if (rect) {
    const top = host.scrollTop, bottom = top + host.clientHeight;
    if (rect.y < top) host.scrollTop = Math.max(0, rect.y - 8);
    else if (rect.y + rect.height > bottom) host.scrollTop = Math.max(0, rect.y + rect.height - host.clientHeight + 8);
  }
  renderWindow();
  if (focusDom) requestAnimationFrame(() => mounted.get(id)?.focus({ preventScroll: true }));
  return true;
}
function currentGalleryId() {
  if (state.focusId != null && state.items.some(item => item.id === state.focusId)) return state.focusId;
  if (state.activeId != null && state.items.some(item => item.id === state.activeId)) return state.activeId;
  if (state.lastClicked != null && state.items.some(item => item.id === state.lastClicked)) return state.lastClicked;
  const selected = [...state.selected].find(id => state.items.some(item => item.id === id));
  return selected ?? state.items[0]?.id ?? null;
}
function moveGalleryFocus(direction) {
  if (!state.items.length) return;
  const current = currentGalleryId();
  let index = state.items.findIndex(item => item.id === current);
  if (index < 0) index = direction > 0 ? -1 : state.items.length;
  index = Math.max(0, Math.min(state.items.length - 1, index + direction));
  focusGalleryId(state.items[index].id);
}
function galleryTargetIds() {
  if (state.selected.size) return [...state.selected].filter(id => state.items.some(item => item.id === id)).slice(0, 500);
  const id = currentGalleryId();
  return id == null ? [] : [id];
}
function likeTagName() {
  return state.facets.find(tag => tag.type === 'custom' && tag.name.toLowerCase() === 'like')?.name || 'like';
}
async function toggleLike(id) {
  const item = state.items.find(value => value.id === id);
  if (!item) return;
  if (!state.writeable) { announce('読み取り専用ではLikeを変更できません'); return; }
  try {
    const next = !item.liked;
    await api('/tags', { ids: [id], name: likeTagName(), action: next ? 'add' : 'remove' });
    item.liked = next;
    renderWindow();
    if (state.detail?.id === id) {
      state.detail = await api('/items/' + id);
      if (!$('inspector').hidden) renderInspector(state.detail);
    }
    if (viewerOpen() && state.viewerId === id) {
      const detail = await api('/items/' + id);
      if (viewerOpen() && state.viewerId === id) renderViewerDetail(detail);
    }
    await refreshFacets();
    announce(next ? 'Likeに追加しました' : 'Likeを解除しました');
  } catch (e) { announce(e.message); }
}
async function applyKeyboardSort(sort, label) {
  const focused = currentGalleryId();
  state.filters.sort = normalizeSort(sort);
  persistSort();
  syncSortControls();
  try {
    await reload(true);
    if (focused != null) focusGalleryId(focused);
    announce(label);
  } catch (e) { announce(e.message); }
}
function applyToolbarSort() {
  const next = sortFromControls();
  if (next === state.filters.sort) { syncSortControls(); return; }
  state.filters.sort = next;
  persistSort();
  syncSortControls();
  void reload();
}


async function handleLibraryDrop(event, dest) {
  const transfer = event.dataTransfer;
  if (!transfer) return;
  const types = Array.from(transfer.types || []);
  const internal = transfer.getData('application/x-iib-media-ids');
  const files = Array.from(transfer.files || []);
  if (!internal && !files.length) return;
  event.preventDefault();
  event.stopPropagation();

  try {
    if (internal) {
      const ids = JSON.parse(internal);
      if (!Array.isArray(ids) || !ids.length) return;
      await api('/move-items', { ids: ids.slice(0, 500).map(Number).filter(Number.isFinite), dest });
      state.selected.clear();
      await reload(true);
      announce(ids.length + ' 件を移動しました');
      return;
    }

    const images = files.filter(file => /^image\//.test(file.type) || /\.(png|jpe?g|gif|bmp|webp|avif)$/i.test(file.name));
    if (!images.length) { announce('画像ファイルだけ追加できます'); return; }
    let done = 0;
    for (const file of images) {
      await uploadDroppedFile(file, dest);
      done += 1;
    }
    announce(done + ' 件を追加しました。索引を更新しています…');
    pollStatus();
  } catch (e) { announce(e.message); }
}
function currentDropFolder() {
  if (state.filters.folder) return state.filters.folder;
  if (state.roots.length === 1 && state.roots[0].available !== false) return state.roots[0].path;
  return '';
}
$('gallery-scroll').addEventListener('dragover', event => {
  if (Array.from(event.dataTransfer?.types || []).includes('Files')) {
    event.preventDefault();
    $('gallery-scroll').classList.add('drop-target');
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  }
});
$('gallery-scroll').addEventListener('dragleave', () => $('gallery-scroll').classList.remove('drop-target'));
$('gallery-scroll').addEventListener('drop', event => {
  $('gallery-scroll').classList.remove('drop-target');
  if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
  const dest = currentDropFolder();
  if (!dest) { event.preventDefault(); announce('追加先フォルダを左のフォルダ一覧から選んでください'); return; }
  void handleLibraryDrop(event, dest);
});

$('gallery-scroll').addEventListener('scroll', () => {
  if (scrollFrame) return;
  scrollFrame = requestAnimationFrame(() => { scrollFrame = 0; renderWindow(); });
}, { passive: true });
new ResizeObserver(() => {
  if (Math.abs($('gallery-scroll').clientWidth - 16 - state.width) > 1) layout(captureAnchor());
  else renderWindow();
}).observe($('gallery-scroll'));

async function reload(preserve = false) {
  const requestId = ++state.request;
  const anchor = preserve ? captureAnchor() : null;
  state.loading = true;
  if (!preserve) { closeInspector(); state.selected.clear(); $('gallery-scroll').scrollTop = 0; }
  try {
    const result = await api('/search', { ...state.filters, cursor: '', limit: 120 });
    if (requestId !== state.request) return;
    state.items = result.items; state.cursor = result.cursor; state.hasMore = Boolean(result.cursor); state.total = result.total;
    state.index = result.index;
    for (const tile of mounted.values()) tile.remove(); mounted.clear();
    state.loading = false; updateEmpty(); updateChrome(); layout(anchor);
  } catch (error) {
    if (requestId !== state.request) return;
    state.loading = false; $('empty').hidden = false; $('empty').textContent = error.message;
  }
}
async function loadNext() {
  if (state.loading || !state.hasMore) return;
  const requestId = state.request, cursor = state.cursor; state.loading = true;
  try {
    const result = await api('/search', { ...state.filters, cursor, limit: 120 });
    if (requestId !== state.request) return;
    const ids = new Set(state.items.map(x => x.id)); state.items.push(...result.items.filter(x => !ids.has(x.id)));
    state.cursor = result.cursor; state.hasMore = Boolean(result.cursor) && result.cursor !== cursor;
    state.total = result.total; state.loading = false; layout(); updateChrome();
  } catch (error) { if (requestId === state.request) { state.loading = false; state.hasMore = false; announce(error.message); } }
}
function updateEmpty() {
  const empty = $('empty'); empty.replaceChildren(); empty.hidden = state.items.length > 0;
  if (state.items.length) return;
  if (!state.roots.length) empty.append(button('フォルダを追加', () => showDialog('folder-dialog'), 'primary'));
  else empty.textContent = state.index.running ? '画像を読み取っています…' : '一致する画像がありません';
}

async function refreshFacets() {
  const response = await api('/facets'); state.facets = response.tags;
  renderSidebarTags();
}
function setFolder(path) {
  state.filters.folder = typeof path === 'string' ? path.trim() : '';
  persistSelectedFolder(state.filters.folder); state.filters.tags = [];
  renderFolders(); void reload();
}
function renderFolders() {
  closeFolderContextMenu();
  const list = $('folder-list'); list.replaceChildren();
  function branch(folder, removable = false) {
    const fragment = document.createDocumentFragment();
    const row = el('div', 'folder-row'), children = el('div', 'folder-children');
    row.addEventListener('dragover', event => {
      const types = Array.from(event.dataTransfer?.types || []);
      if (types.includes('Files') || types.includes('application/x-iib-media-ids')) {
        event.preventDefault(); row.classList.add('drop-target');
        if (event.dataTransfer) event.dataTransfer.dropEffect = types.includes('Files') ? 'copy' : 'move';
      }
    });
    row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
    row.addEventListener('drop', event => { row.classList.remove('drop-target'); void handleLibraryDrop(event, folder.path); });
    const opened = state.folderOpen.has(folder.path); children.hidden = !opened;
    const expand = button(opened ? '⌄' : '›', async () => {
      if (state.folderOpen.has(folder.path)) state.folderOpen.delete(folder.path);
      else {
        state.folderOpen.add(folder.path);
        if (!state.folderChildren.has(folder.path)) {
          try { state.folderChildren.set(folder.path, (await api('/folders?path=' + encodeURIComponent(folder.path))).folders); }
          catch (e) { state.folderOpen.delete(folder.path); announce(e.message); }
        }
      }
      renderFolders();
    }, 'folder-expand');
    const folderName = String(folder.label || folderBookmarkLabel(folder.path));
    expand.setAttribute('aria-label', folderName + ' のサブフォルダ（' + folder.path + '）');
    expand.setAttribute('aria-expanded', String(opened));
    const choose = button('', () => setFolder(folder.path), 'folder-item');
    decorateFolderButton(choose, folderName, folder.path);
    choose.classList.toggle('active', normalizeFolderPath(state.filters.folder) === normalizeFolderPath(folder.path));
    if (folder.available === false) choose.append(el('span', 'unavailable', '未接続'));
    row.append(expand, choose);
    bindFolderContextMenu(row, choose, folder.path);
    if (removable) {
      const remove = button('×', async () => {
        if (!confirm('参照対象から外します。元ファイルは削除しません。')) return;
        try { const res = await api('/roots', { path: folder.path, action: 'remove' }); state.roots = res.roots; state.filters.folder = ''; persistSelectedFolder(''); renderFolders(); void reload(); } catch (e) { announce(e.message); }
      }, 'folder-remove');
      remove.setAttribute('aria-label', folderName + '（' + folder.path + '）を参照から外す'); row.append(remove);
    }
    if (opened) {
      const cached = state.folderChildren.get(folder.path) || [];
      for (const child of cached) children.append(branch(child));
      if (!cached.length) children.append(el('span', 'quiet', 'サブフォルダなし'));
    }
    fragment.append(row, children); return fragment;
  }
  for (const root of state.roots) list.append(branch(root, true));
  const bookmarks = $('folder-bookmarks');
  if (bookmarks) {
    bookmarks.replaceChildren();
    for (const path of state.folderBookmarks) {
      const row = el('div', 'folder-row folder-bookmark-row');
      const label = folderBookmarkLabel(path), choose = button('', () => setFolder(path), 'folder-item');
      decorateFolderButton(choose, label, path);
      choose.classList.toggle('active', normalizeFolderPath(state.filters.folder) === normalizeFolderPath(path));
      if (!folderBookmarkAvailable(path)) choose.append(el('span', 'unavailable', '未接続'));
      row.append(choose); bindFolderContextMenu(row, choose, path, 'remove'); bookmarks.append(row);
    }
    if (!state.folderBookmarks.length) bookmarks.append(el('span', 'quiet', 'フォルダを右クリックして追加できます'));
  }
  updateChrome();
}
function renderSidebarTags() {
  const list = $('sidebar-tags'); list.replaceChildren();
  const tags = state.facets.filter(t => t.type === 'custom');
  for (const tag of tags.slice(0, 120)) {
    const b = button(tag.name, () => { const set = new Set(state.filters.tags); set.has(tag.id) ? set.delete(tag.id) : set.add(tag.id); state.filters.tags = [...set]; renderSidebarTags(); void reload(); }, 'tag-choice');
    b.classList.toggle('active', state.filters.tags.includes(tag.id)); list.append(b);
  }
  if (!tags.length) list.append(el('span', 'quiet', '画像の詳細から追加できます'));
  $('custom-tag-options').replaceChildren(...tags.map(t => { const o = el('option'); o.value = t.name; return o; }));
}

async function openInspector(id) {
  const requestId = ++state.detailRequest; state.activeId = id;
  const anchor = captureAnchor(); $('inspector').hidden = false; $('inspector-content').replaceChildren(el('p', 'quiet', '読み込み中…'));
  layout(anchor);
  try {
    const detail = await api('/items/' + id);
    if (requestId !== state.detailRequest) return;
    state.detail = detail; renderInspector(detail); renderWindow();
  } catch (e) { if (requestId === state.detailRequest) $('inspector-content').replaceChildren(el('p', 'error', e.message)); }
}
function closeInspector() {
  state.detailRequest++; state.activeId = null; const anchor = captureAnchor(); $('inspector').hidden = true; layout(anchor);
}
function metaSection(label) { const section = el('section', 'meta-section'); section.append(el('h3', '', label)); return section; }
function promptBlock(label, value) {
  const section = metaSection(label), heading = section.firstChild;
  if (value) { const copy = button('コピー', () => copyText(value), 'quiet-button'); heading.append(copy); }
  section.append(el('pre', 'prompt-text' + (value ? '' : ' absent'), value || '記録なし'));
  return section;
}
async function copyText(value) { try { await navigator.clipboard.writeText(value); announce('コピーしました'); } catch { announce('コピーできません。文字列を選択してコピーしてください。'); } }
async function setRating(ids, rating) {
  if (!ids.length) return;
  if (!state.writeable) { announce('読み取り専用ではレーティングを変更できません'); return; }
  try {
    await api('/ratings', { ids: ids.slice(0, 500), rating });
    const idSet = new Set(ids);
    for (const item of state.items) if (idSet.has(item.id)) item.rating = rating;
    if (state.detail && idSet.has(state.detail.id)) state.detail.rating = rating;
    renderWindow();
    if (state.detail && idSet.has(state.detail.id) && !$('inspector').hidden) renderInspector(state.detail);
    if (viewerOpen() && state.viewerId && idSet.has(state.viewerId)) {
      const detail = await api('/items/' + state.viewerId);
      if (viewerOpen() && state.viewerId === detail.id) renderViewerDetail(detail);
    }
    announce(rating ? 'レーティングを ' + rating + ' に設定しました' : 'レーティングを解除しました');
  } catch (e) { announce(e.message); }
}
function changeRatingMax(value) {
  state.ratingMax = Number(value) === 10 ? 10 : 5;
  localStorage.setItem('iib:library:rating-max', String(state.ratingMax));
  if (state.detail && !$('inspector').hidden) renderInspector(state.detail);
  if (viewerOpen() && state.viewerId) void api('/items/' + state.viewerId).then(renderViewerDetail).catch(e => announce(e.message));
}
function ratingControl(data, className = '') {
  const wrap = el('section', ('rating-control ' + className).trim());
  const head = el('div', 'rating-heading');
  head.append(el('strong', '', 'レーティング ' + (data.rating || 0) + '/' + state.ratingMax));
  const select = el('select', 'rating-max');
  select.setAttribute('aria-label', '最大レーティング');
  for (const value of [5, 10]) {
    const option = el('option', '', value + '段階');
    option.value = value;
    option.selected = state.ratingMax === value;
    select.append(option);
  }
  select.addEventListener('change', e => changeRatingMax(e.target.value));
  head.append(select);
  wrap.append(head);
  const stars = el('div', 'rating-stars');
  for (let value = 1; value <= state.ratingMax; value++) {
    const b = button('★', () => void setRating([data.id], value), 'rating-star');
    b.classList.toggle('on', (data.rating || 0) >= value);
    b.title = value + ' に設定';
    b.setAttribute('aria-label', 'レーティング ' + value);
    stars.append(b);
  }
  wrap.append(stars);
  const footer = el('div', 'rating-footer');
  footer.append(el('small', 'quiet', state.ratingMax === 10 ? '数字キー 1–9 / 0=10' : '数字キー 1–5'));
  const clear = button('解除', () => void setRating([data.id], 0), 'quiet-button');
  clear.disabled = !data.rating;
  footer.append(clear);
  wrap.append(footer);
  return wrap;
}
function renderInspector(data) {
  const content = $('inspector-content'); content.replaceChildren(); content.scrollTop = 0;
  const preview = el('div', 'detail-preview');
  const media = el(data.kind === 'video' ? 'video' : 'img');
  media.src = fileUrl(data.id, data.kind === 'video' ? 'file' : 'thumbnail', 1280);
  if (data.kind === 'video') { media.controls = true; media.preload = 'metadata'; media.poster = fileUrl(data.id); }
  else { media.alt = '選択した画像'; media.addEventListener('dblclick', () => openViewer(data.id, true)); }
  preview.append(media);
  const expand = button('', () => openViewer(data.id), 'preview-expand'); expand.append(icon('expand')); expand.setAttribute('aria-label', '原寸プレビュー'); preview.append(expand);
  content.append(preview);
  const stamp = new Date(data.modified).toLocaleString('ja-JP', { year:'numeric', month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' });
  content.append(el('p', 'file-summary', `${data.width} × ${data.height} · ${stamp}`));
  content.append(ratingControl(data, 'inspector-rating'));
  const identity = metaSection('モデル');
  identity.append(el('p', 'model-name', data.model || '記録なし'));
  if (data.source) identity.append(el('span', 'source-label', data.source));
  if (data.loras.length) { identity.append(el('h4', '', 'LoRA')); for (const lora of data.loras) { const row = el('div', 'lora-row'); row.append(el('span', '', lora.name)); if (lora.strength !== undefined && lora.strength !== null) row.append(el('small', 'quiet', lora.strength)); identity.append(row); } }
  content.append(identity);
  content.append(promptBlock(data.characters.length ? '共通プロンプト' : 'プロンプト', data.positive));
  content.append(promptBlock(data.characters.length ? '共通ネガティブプロンプト' : 'ネガティブプロンプト', data.negative));
  for (const character of data.characters) {
    const group = el('section', 'character-group'); group.append(el('h3', 'character-title', 'キャラクター ' + character.index));
    if (character.centers.length) group.append(el('p', 'quiet positions', '位置 ' + character.centers.map(p => `(${p.x}, ${p.y})`).join(' · ')));
    group.append(promptBlock('プロンプト', character.positive), promptBlock('ネガティブプロンプト', character.negative)); content.append(group);
  }
  const tagsSection = metaSection('タグ'), tags = el('div', 'detail-tags');
  for (const tag of data.tags) {
    const chip = el('span', 'tag-chip', tag.name);
    const remove = button('×', async () => { try { await api('/tags', { ids: [data.id], name: tag.name, action: 'remove' }); await openInspector(data.id); await refreshFacets(); } catch (e) { announce(e.message); } }, 'remove-tag');
    remove.setAttribute('aria-label', tag.name + ' を外す'); chip.append(remove); tags.append(chip);
  }
  const form = el('form', 'tag-editor'), input = el('input'); input.placeholder = 'タグを追加'; input.maxLength = 128; input.setAttribute('list', 'custom-tag-options'); input.setAttribute('aria-label', 'タグを追加');
  const add = el('button', '', '＋'); add.type = 'submit'; add.setAttribute('aria-label', 'タグを保存'); form.append(input, add);
  form.addEventListener('submit', async event => { event.preventDefault(); if (!input.value.trim()) return; try { await api('/tags', { ids: [data.id], name: input.value.trim(), action: 'add' }); await openInspector(data.id); await refreshFacets(); } catch (e) { announce(e.message); } });
  tagsSection.append(tags, form); content.append(tagsSection);
  const technical = metaSection('生成パラメーター'), values = el('dl', 'technical-values');
  const labels = { seed:'Seed', noise_seed:'Seed', sampler:'Sampler', sampler_name:'Sampler', steps:'Steps', scale:'CFG', cfg:'CFG', scheduler:'Scheduler', noise_schedule:'Scheduler', denoise:'Denoise' };
  const used = new Set();
  for (const [name,value] of Object.entries(data.technical || {})) {
    const label = labels[name.toLowerCase()] || name;
    if (used.has(label.toLowerCase())) continue; used.add(label.toLowerCase());
    values.append(el('dt', '', label), el('dd', '', value));
  }
  if (values.childElementCount) { technical.append(values); content.append(technical); }
  const actions = el('div', 'detail-actions');
  const download = el('a', 'control', 'ダウンロード'); download.href = fileUrl(data.id, 'file') + '?download=true'; download.setAttribute('download', data.name);
  actions.append(download, button('パスをコピー', () => copyText(data.path), 'control'));
  content.append(actions, el('p', 'detail-path', data.path));
  if (data.stale) content.append(el('p', 'error', '元ファイルが更新されています。索引を更新してください。'));
}

function showDialog(id) { previousFocus = document.activeElement; $(id).showModal(); }
for (const b of document.querySelectorAll('[data-close]')) b.addEventListener('click', () => $(b.dataset.close).close());
for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('close', () => { if (previousFocus?.isConnected) previousFocus.focus(); });
async function openFilters() {
  try { await refreshFacets(); } catch (e) { announce(e.message); }
  const form = $('filters');
  const options = (id, values, current) => { const select = $(id); select.replaceChildren(el('option', '', 'すべて')); select.firstChild.value = ''; for (const [value,label] of values) { const option = el('option', '', label); option.value = value; select.append(option); } select.value = current; };
  const folderOptions = state.roots.map(r => [r.path, folderOptionLabel(r.label, r.path)]);
  if (state.filters.folder && !folderOptions.some(x => x[0] === state.filters.folder)) folderOptions.push([state.filters.folder, folderOptionLabel('', state.filters.folder)]);
  options('filter-folder', folderOptions, state.filters.folder);
  options('filter-source', state.facets.filter(t => t.type === 'Source Identifier').map(t => [t.name,t.name]), state.filters.source);
  for (const field of ['kind','model','lora']) form.elements[field].value = state.filters[field];
  for (const [id,type] of [['model-options','Model'],['lora-options','lora']]) $(id).replaceChildren(...state.facets.filter(t => t.type === type).map(t => { const o=el('option'); o.value=t.name; return o; }));
  $('filter-tags').replaceChildren();
  for (const tag of state.facets.filter(t => t.type === 'custom')) { const label = el('label', 'check-label'), check = el('input'); check.type='checkbox'; check.name='tag'; check.value=tag.id; check.checked=state.filters.tags.includes(tag.id); label.append(check, document.createTextNode(tag.name)); $('filter-tags').append(label); }
  if (!$('filter-tags').childElementCount) $('filter-tags').append(el('span','quiet','登録タグはありません'));
  showDialog('filter-dialog');
}
$('filters').addEventListener('submit', event => { event.preventDefault(); const values = new FormData(event.currentTarget); for (const field of ['folder','source','model','lora','kind']) state.filters[field] = String(values.get(field) || '').trim(); persistSelectedFolder(state.filters.folder); state.filters.tags = values.getAll('tag').map(Number); $('filter-dialog').close(); renderFolders(); renderSidebarTags(); void reload(); });
$('reset-filters').addEventListener('click', () => { Object.assign(state.filters, { folder:'',source:'',model:'',lora:'',tags:[],kind:'all' }); persistSelectedFolder(''); $('filter-dialog').close(); renderFolders(); renderSidebarTags(); void reload(); });
$('folder-form').addEventListener('submit', async event => { event.preventDefault(); $('folder-error').textContent=''; try { const result=await api('/roots',{ path:$('folder-path').value.trim(),action:'add' }); state.roots=result.roots; $('folder-dialog').close(); $('folder-path').value=''; renderFolders(); await reload(); pollStatus(); } catch (e) { $('folder-error').textContent=e.message; } });
async function openTagEditor(ids) {
  const unique = [...new Set(ids)].filter(id => state.items.some(item => item.id === id) || id === state.viewerId).slice(0, 500);
  if (!unique.length) { announce('タグを編集する画像を選択してください'); return; }
  if (!state.writeable) { announce('読み取り専用ではタグを変更できません'); return; }
  try {
    const membership = await api('/tag-membership', { ids: unique });
    state.tagEditorIds = unique;
    $('tag-edit-summary').textContent = unique.length + ' 件のタグをまとめて編集';
    $('batch-tag-name').value = '';
    const list = $('tag-edit-list'); list.replaceChildren();
    for (const tag of membership.tags) {
      const label = el('label', 'tag-edit-choice');
      const check = el('input'); check.type = 'checkbox'; check.dataset.tagName = tag.name;
      check.checked = tag.applied === membership.total;
      check.indeterminate = tag.applied > 0 && tag.applied < membership.total;
      check.addEventListener('change', () => { check.indeterminate = false; check.dataset.changed = 'true'; });
      const caption = el('span', '', tag.name);
      if (tag.applied > 0 && tag.applied < membership.total) caption.append(el('small', 'quiet', ' ' + tag.applied + '/' + membership.total));
      label.append(check, caption); list.append(label);
    }
    if (!membership.tags.length) list.append(el('span', 'quiet', '登録タグはありません'));
    showDialog('tag-dialog');
  } catch (e) { announce(e.message); }
}
$('tag-form').addEventListener('submit', async event => {
  event.preventDefault();
  const ids = state.tagEditorIds.slice(0, 500);
  if (!ids.length) return;
  const focused = viewerOpen() ? state.viewerId : currentGalleryId();
  try {
    const operations = [];
    for (const check of $('tag-edit-list').querySelectorAll('input[data-changed="true"]')) {
      operations.push(api('/tags', { ids, name: check.dataset.tagName, action: check.checked ? 'add' : 'remove' }));
    }
    const newName = $('batch-tag-name').value.trim();
    if (newName) operations.push(api('/tags', { ids, name: newName, action: 'add' }));
    await Promise.all(operations);
    $('tag-dialog').close();
    state.tagEditorIds = [];
    await refreshFacets();
    await reload(true);
    if (focused != null) focusGalleryId(focused, !viewerOpen());
    if (state.activeId != null && !$('inspector').hidden) await openInspector(state.activeId);
    if (viewerOpen() && state.viewerId != null) {
      const detail = await api('/items/' + state.viewerId);
      if (viewerOpen() && state.viewerId === detail.id) renderViewerDetail(detail);
    }
    announce(operations.length ? 'タグを更新しました' : '変更はありません');
  } catch(e) { announce(e.message); }
});

function viewerOpen() { return !$('viewer').hidden; }
function viewerFullscreenTarget() { return $('viewer'); }
function showViewer() {
  viewerPreviousFocus = document.activeElement;
  const viewer = $('viewer');
  viewer.hidden = false;
  viewer.setAttribute('open', '');
}
async function requestViewerFullscreen() {
  const target = viewerFullscreenTarget();
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else if (target?.requestFullscreen) await target.requestFullscreen();
    else announce('このブラウザでは全画面表示を利用できません');
  } catch { announce('全画面表示に切り替えられませんでした'); }
}
function viewerTextSection(label, value) {
  const section = el('section', 'viewer-meta-section');
  section.append(el('h3', '', label), el('pre', 'viewer-meta-text', value || '記録なし'));
  return section;
}
function renderViewerDetail(data) {
  if (!viewerOpen() || data.id !== state.viewerId) return;
  const left = $('viewer-left'), right = $('viewer-right');
  left.replaceChildren(); right.replaceChildren();
  left.append(el('h2', 'viewer-name', data.name), ratingControl(data, 'viewer-rating'));
  const stamp = new Date(data.modified).toLocaleString('ja-JP', { year:'numeric', month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit' });
  left.append(el('p', 'viewer-file-summary', data.width + ' × ' + data.height + '\n' + stamp + '\n' + data.kind));
  const identity = el('section', 'viewer-meta-section');
  identity.append(el('h3', '', 'モデル / Source'), el('p', 'viewer-meta-value', data.model || '記録なし'));
  if (data.source) identity.append(el('small', 'quiet', data.source));
  if (data.loras?.length) {
    identity.append(el('h4', '', 'LoRA'));
    for (const lora of data.loras) identity.append(el('p', 'viewer-meta-value', lora.name + (lora.strength != null ? ' (' + lora.strength + ')' : '')));
  }
  left.append(identity);
  const tags = el('section', 'viewer-meta-section'); tags.append(el('h3', '', 'タグ'));
  const chips = el('div', 'detail-tags');
  for (const tag of data.tags || []) chips.append(el('span', 'tag-chip', tag.name));
  if (!chips.childElementCount) chips.append(el('span', 'quiet', 'なし'));
  tags.append(chips); left.append(tags);
  left.append(viewerTextSection('ファイル', data.path));

  right.append(viewerTextSection(data.characters?.length ? '共通プロンプト' : 'プロンプト', data.positive));
  right.append(viewerTextSection(data.characters?.length ? '共通ネガティブ' : 'ネガティブプロンプト', data.negative));
  for (const character of data.characters || []) {
    right.append(viewerTextSection('キャラクター ' + character.index, character.positive));
    right.append(viewerTextSection('キャラクター ' + character.index + ' ネガティブ', character.negative));
  }
  if (data.technical && Object.keys(data.technical).length) {
    const technical = el('section', 'viewer-meta-section');
    technical.append(el('h3', '', '生成パラメーター'));
    const dl = el('dl', 'technical-values');
    for (const [key, value] of Object.entries(data.technical)) dl.append(el('dt', '', key), el('dd', '', value));
    technical.append(dl); right.append(technical);
  }
}
function setViewerLoading(loading) {
  const content = $('viewer-content');
  if (loading) content.setAttribute('aria-busy', 'true');
  else content.removeAttribute('aria-busy');
  $('previous-image').disabled = loading;
  $('next-image').disabled = loading;
}
function createViewerMedia(item) {
  const media = el(item.kind === 'video' ? 'video' : 'img');
  if (item.kind === 'video') {
    media.controls = true;
    media.autoplay = false;
    media.preload = 'auto';
    media.playsInline = true;
  } else {
    media.alt = '原寸プレビュー';
    media.decoding = 'async';
  }
  media.src = fileUrl(item.id, 'file');
  return media;
}
async function waitForViewerMedia(media, kind) {
  if (kind === 'video') {
    if (media.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) return;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(new Error('動画の読み込みがタイムアウトしました')), 15000);
      const cleanup = () => {
        clearTimeout(timer);
        media.removeEventListener('loadeddata', loaded);
        media.removeEventListener('error', failed);
      };
      const finish = error => { cleanup(); error ? reject(error) : resolve(); };
      const loaded = () => finish();
      const failed = () => finish(new Error('動画を読み込めませんでした'));
      media.addEventListener('loadeddata', loaded, { once:true });
      media.addEventListener('error', failed, { once:true });
      media.load();
    });
    return;
  }

  if (!media.complete || media.naturalWidth === 0) {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(new Error('画像の読み込みがタイムアウトしました')), 15000);
      const cleanup = () => {
        clearTimeout(timer);
        media.removeEventListener('load', loaded);
        media.removeEventListener('error', failed);
      };
      const finish = error => { cleanup(); error ? reject(error) : resolve(); };
      const loaded = () => finish();
      const failed = () => finish(new Error('画像を読み込めませんでした'));
      media.addEventListener('load', loaded, { once:true });
      media.addEventListener('error', failed, { once:true });
    });
  }
  if (typeof media.decode === 'function') {
    try { await media.decode(); } catch { /* Loaded images can still paint if decode() rejects. */ }
  }
}
async function openViewer(id, enterFullscreen = false) {
  const item = state.items.find(x => x.id === id);
  if (!item) return;

  const wasOpen = viewerOpen();
  const requestId = ++state.viewerRequest;
  const content = $('viewer-content');
  const oldVideo = content.querySelector('video');
  if (oldVideo) oldVideo.pause();
  const media = createViewerMedia(item);

  $('viewer').classList.remove('chrome-hidden');
  setViewerLoading(true);

  // Keep the currently visible media mounted during next/previous navigation.
  // The replacement is swapped in only after it is paint-ready.
  if (!wasOpen) {
    state.viewerId = id;
    state.focusId = id;
    renderWindow();
    content.replaceChildren(media);
    state.viewerScale = 1;
    $('viewer-left').replaceChildren(el('p', 'quiet', '読み込み中…'));
    $('viewer-right').replaceChildren();
    showViewer();
  }
  if (enterFullscreen && !document.fullscreenElement) void requestViewerFullscreen();

  try {
    await waitForViewerMedia(media, item.kind);
  } catch (e) {
    if (requestId === state.viewerRequest) {
      setViewerLoading(false);
      announce(e.message);
      if (!wasOpen) $('viewer-left').replaceChildren(el('p', 'error', e.message));
    }
    return;
  }

  if (requestId !== state.viewerRequest || !viewerOpen()) return;

  if (wasOpen) {
    state.viewerId = id;
    state.focusId = id;
    renderWindow();
    content.replaceChildren(media);
    state.viewerScale = 1;
    $('viewer-left').replaceChildren(el('p', 'quiet', '読み込み中…'));
    $('viewer-right').replaceChildren();
  }
  setViewerLoading(false);

  try {
    const detail = await api('/items/' + id);
    if (requestId === state.viewerRequest && state.viewerId === id) renderViewerDetail(detail);
  } catch (e) {
    if (requestId === state.viewerRequest) $('viewer-left').replaceChildren(el('p', 'error', e.message));
  }
}
function stepViewer(direction) {
  if ($('viewer-content').getAttribute('aria-busy') === 'true') return;
  const index = state.items.findIndex(x => x.id === state.viewerId);
  const next = state.items[index + direction];
  if (next) void openViewer(next.id);
}
$('viewer-content').addEventListener('click', event => {
  if (event.target instanceof HTMLImageElement) {
    $('viewer').classList.toggle('chrome-hidden');
  }
});
$('viewer-content').addEventListener('wheel', event => {
  const image = $('viewer-content').querySelector('img'); if (!image) return;
  event.preventDefault();
  state.viewerScale = Math.max(1, Math.min(5, state.viewerScale + (event.deltaY < 0 ? .25 : -.25)));
  image.style.transform = 'scale(' + state.viewerScale + ')';
}, { passive:false });
function cleanupViewer() {
  const viewedId = state.viewerId;
  state.viewerRequest++; state.viewerId = null;
  setViewerLoading(false);
  const video = $('viewer-content').querySelector('video'); if (video) video.pause();
  $('viewer-content').replaceChildren(); $('viewer-left').replaceChildren(); $('viewer-right').replaceChildren();
  const viewer = $('viewer'); viewer.hidden = true; viewer.removeAttribute('open'); viewer.classList.remove('chrome-hidden');
  if (!focusGalleryId(viewedId) && viewerPreviousFocus?.isConnected) viewerPreviousFocus.focus();
}
async function closeViewer() {
  if (document.fullscreenElement === viewerFullscreenTarget()) { try { await document.exitFullscreen(); } catch {} }
  if (viewerOpen()) cleanupViewer();
}
$('close-viewer').addEventListener('click', () => void closeViewer());
$('viewer-fullscreen').addEventListener('click', () => void requestViewerFullscreen());
$('previous-image').addEventListener('click', () => stepViewer(-1));
$('next-image').addEventListener('click', () => stepViewer(1));
document.addEventListener('fullscreenchange', () => {
  $('viewer-fullscreen').title = document.fullscreenElement ? '全画面を解除 (F)' : '全画面表示 (F)';
});
function shortcutBlocked() {
  const target = document.activeElement;
  return target && (target.matches('input,textarea,select,[contenteditable="true"]') || target.closest('[contenteditable="true"]'));
}
document.addEventListener('keydown', event => {
  if (folderContextMenuOpen() && event.key === 'Escape') {
    event.preventDefault(); closeFolderContextMenu(true); return;
  }
  if (event.key === 'ContextMenu' || (event.key === 'F10' && event.shiftKey)) {
    const target = event.target instanceof Element ? event.target.closest('[data-folder-path]') : null;
    if (target?.dataset.folderPath) {
      event.preventDefault();
      const row = target.closest('.folder-row') || target, bounds = row.getBoundingClientRect();
      openFolderContextMenu(target.dataset.folderPath, row, bounds.left + Math.min(bounds.width, 180), bounds.bottom, row.classList.contains('folder-bookmark-row') ? 'remove' : 'auto');
      return;
    }
  }
  if (shortcutBlocked()) return;
  if (document.querySelector('dialog[open]')) return;

  if (!viewerOpen() && event.key === 'F8') {
    event.preventDefault(); void applyKeyboardSort('name', '名前（昇順）に並べ替えました'); return;
  }
  if (!viewerOpen() && event.key === 'F9') {
    event.preventDefault(); void applyKeyboardSort('newest', '更新日時（降順・新しい順）に並べ替えました'); return;
  }

  if (event.key === 'f' || event.key === 'F') {
    event.preventDefault();
    if (viewerOpen()) { void requestViewerFullscreen(); return; }
    const id = currentGalleryId();
    if (id != null) void openViewer(id, true);
    return;
  }

  if (event.key === 'a' || event.key === 'A') {
    const id = viewerOpen() ? state.viewerId : currentGalleryId();
    if (id != null) { event.preventDefault(); void toggleLike(id); }
    return;
  }

  if (event.key === 'F3') {
    const ids = viewerOpen() && state.viewerId != null ? [state.viewerId] : galleryTargetIds();
    if (ids.length) { event.preventDefault(); void openTagEditor(ids); }
    return;
  }

  const rating = ratingFromKey(event.key, state.ratingMax);
  if (rating !== null) {
    const ids = shortcutTargetIds({ viewerId:viewerOpen() ? state.viewerId : null, selected:[...state.selected], activeId:state.activeId, lastClicked:state.lastClicked });
    if (ids.length) { event.preventDefault(); void setRating(ids, rating); }
    return;
  }

  if (viewerOpen()) {
    if (event.key === 'Escape') {
      event.preventDefault();
      if (document.fullscreenElement === viewerFullscreenTarget()) void document.exitFullscreen();
      else void closeViewer();
      return;
    }
    if (event.key === 'ArrowRight') { event.preventDefault(); stepViewer(1); }
    if (event.key === 'ArrowLeft') { event.preventDefault(); stepViewer(-1); }
    return;
  }

  if (event.key === 'ArrowRight') { event.preventDefault(); moveGalleryFocus(1); return; }
  if (event.key === 'ArrowLeft') { event.preventDefault(); moveGalleryFocus(-1); return; }
  if (event.key === 'Escape') { closeInspector(); $('tools-menu').hidden = true; }
  if (event.key === '/') { event.preventDefault(); $('search').focus(); }
});
function toolsMenu() {
  const menu=$('tools-menu'); menu.replaceChildren();
  if(embedded){for(const [section,label] of [['generated','73の生成物'],['assets','参照素材'],['presets','プリセット'],['templates','テンプレート'],['memory','Creative Memory']]){
    const b=button(label,()=>{menu.hidden=true;window.parent.postMessage({source:'iib-media-library',action:'open-section',section},location.origin);});b.setAttribute('role','menuitem');menu.append(b);
  }}
  const classic=el('a','','従来のIIB機能');classic.href=base;classic.target='_blank';classic.rel='noopener';classic.setAttribute('role','menuitem');menu.append(classic);
  menu.hidden=!menu.hidden; $('tools-button').setAttribute('aria-expanded',String(!menu.hidden));
  if(!menu.hidden)menu.firstElementChild?.focus();
}
document.addEventListener('click',e=>{if(!$('tools-menu').contains(e.target)&&!$('tools-button').contains(e.target)){$('tools-menu').hidden=true;$('tools-button').setAttribute('aria-expanded','false');}});
document.addEventListener('click', event => {
  const menu = $('folder-context-menu');
  if (menu && !menu.contains(event.target)) closeFolderContextMenu();
});
window.addEventListener('scroll', () => closeFolderContextMenu(), true);
window.addEventListener('resize', () => closeFolderContextMenu());
window.addEventListener('message',event=>{if(event.origin===location.origin&&event.source===window.parent&&event.data?.source==='generation-studio'&&event.data.action==='theme'){document.documentElement.dataset.theme=event.data.theme==='light'?'light':'dark';}});

async function pollStatus() {
  clearTimeout(statusTimer);
  try {
    const status=await api('/status');state.index=status;
    $('index-status').hidden=!status.running&&!status.failure&&!status.errors;
    $('index-status').textContent=status.failure||(status.running?`索引を更新中 · ${status.seen.toLocaleString()} 件確認`:status.errors?`${status.errors} 件を読み取れませんでした。索引更新で再試行できます。`:'');
    if(!state.items.length&&status.indexed&&!state.loading)await reload(true);
    if(!status.running&&status.finished_at&&state.observedFinish!==status.finished_at){state.observedFinish=status.finished_at;if($('gallery-scroll').scrollTop < 30 || !state.items.length)await reload(true);else{state.pendingRefresh=true;$('refresh').title='新しい画像があります。クリックで先頭から更新';}await refreshFacets();}
    updateEmpty();
  }catch{ /* 一時的なstatus取得失敗で表示中の画像を消さない。 */ }
  statusTimer=setTimeout(pollStatus,state.index.running?1500:12000);
}
async function refreshIndex() { try { state.pendingRefresh=false;state.index=await api('/index',{});$('gallery-scroll').scrollTop=0;await reload();pollStatus(); } catch(e){announce(e.message);} }
async function boot() {
  for(const [id,name] of [['toggle-folders','folders'],['search-icon','search'],['filter-icon','filter'],['refresh','refresh'],['size-icon','size']])$(id).append(icon(name));
  $('image-size').value=state.target;
  if(innerWidth<760)state.sidebar=false;
  updateChrome();
  try {
    const config=await api('/config');state.roots=config.roots;state.filters.folder=restoreSelectedFolder(state.roots);state.index=config.index;state.writeable=config.writeable;
    renderFolders();await reload();await refreshFacets();
    if(config.writeable)state.index=await api('/index',{});
    pollStatus();
  } catch(e){$('empty').hidden=false;$('empty').textContent=e.message;}
  if(embedded)window.parent.postMessage({source:'iib-media-library',action:'ready'},location.origin);
}
$('toggle-folders').addEventListener('click',()=>{const anchor=captureAnchor();state.sidebar=!state.sidebar;localStorage.setItem('iib:library:sidebar',String(state.sidebar));updateChrome();layout(anchor);});
$('image-size').addEventListener('input',event=>{const anchor=captureAnchor();state.target=Number(event.target.value);localStorage.setItem('iib:library:size',String(state.target));layout(anchor);});
$('filter-button').addEventListener('click',openFilters);$('add-folder').addEventListener('click',()=>showDialog('folder-dialog'));
$('all-media').addEventListener('click',()=>setFolder(''));$('close-inspector').addEventListener('click',closeInspector);
$('search-form').addEventListener('submit',event=>{event.preventDefault();clearTimeout(searchTimer);state.filters.q=$('search').value.trim();void reload();});
$('search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{state.filters.q=$('search').value.trim();void reload();},300);});
$('sort-field').addEventListener('change',applyToolbarSort);
$('sort-direction').addEventListener('change',applyToolbarSort);
$('refresh').addEventListener('click',refreshIndex);$('tools-button').addEventListener('click',toolsMenu);
$('selection-clear').addEventListener('click',()=>{state.selected.clear();updateChrome();});
$('selection-tag').addEventListener('click',()=>{if(state.selected.size>500){announce('一度に編集できるタグは500件までです');return;}void openTagEditor([...state.selected]);});
$('load-more').addEventListener('click',loadNext);
// 閲覧中の新規生成物を低頻度で反映。重複scanはサーバーで拒否する。
setInterval(()=>{if(!document.hidden&&state.writeable&&!state.index.running)void api('/index',{}).then(value=>{state.index=value;pollStatus();}).catch(()=>{});},60000);
void boot();
