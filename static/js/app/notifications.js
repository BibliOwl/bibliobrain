function _isExternalApiPermissionMessage(message) {
  const normalized = String(message || '')
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase();
  return (
    /(autorisation|permission).*(api|appels?|requetes?).*extern/.test(normalized)
    || /(api|appels?|requetes?).*extern.*(requise|required|desactive|activez|enable)/.test(normalized)
    || /external api.*(permission|required|disabled|enable)/.test(normalized)
  );
}

function showToast(msg, duration = 3000, type = '') {
  const el = document.getElementById('toast');
  el.classList.remove('show', 'toast-success', 'toast-error', 'toast-info');
  const icons = { success: '✓', error: '✕', info: 'i' };
  const displayMsg = window.BibliobrainI18n?.translateRaw
    ? window.BibliobrainI18n.translateRaw(String(msg || ''))
    : msg;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  el.setAttribute('aria-live', type === 'error' ? 'assertive' : 'polite');
  el.textContent = (icons[type] ? icons[type] + '  ' : '') + displayMsg;
  const showExternalApiAction = _isExternalApiPermissionMessage(displayMsg)
    && typeof openExternalApiSettings === 'function';
  if (showExternalApiAction) {
    const action = document.createElement('button');
    action.type = 'button';
    action.className = 'toast-settings-btn';
    action.textContent = window.BibliobrainI18n?.t?.('settings.openExternalApis') || 'Ouvrir les réglages';
    action.addEventListener('click', () => {
      el.classList.remove('show');
      openExternalApiSettings();
    });
    el.appendChild(action);
    duration = Math.max(duration, 8000);
  }
  if (type) el.classList.add('toast-' + type);
  el.classList.toggle('toast-actionable', showExternalApiAction);
  void el.offsetWidth;
  el.classList.add('show');
  clearTimeout(el._toastTimer);
  el._toastTimer = setTimeout(() => el.classList.remove('show', 'toast-success', 'toast-error', 'toast-info', 'toast-actionable'), duration);
}

function _showSettingSaved(msg) {
  const text = String(msg || '').trim();
  if (!text) return;
  showToast(text, 1800, 'success');
}

/* ================================================================
 *  Suppression avec annulation (undo)
 *  L'élément est retiré de l'UI immédiatement ; l'appel API ne part
 *  qu'à l'expiration du toast, sauf si l'utilisateur annule.
 * ================================================================ */
const _pendingUndo = new Map();

function flushPendingUndo(key = null) {
  const entries = key ? [[key, _pendingUndo.get(key)]].filter(([, v]) => v) : [..._pendingUndo.entries()];
  entries.forEach(([k, entry]) => {
    clearTimeout(entry.timer);
    _pendingUndo.delete(k);
    entry.commit();
  });
}

function deleteWithUndo(key, { message, restore, commit, duration = 5000 }) {
  // Une seule suppression en attente par type : la précédente est validée.
  flushPendingUndo(key);
  const el = document.getElementById('toast');
  if (!el) { commit(); return; }
  const entry = { commit, timer: 0 };
  _pendingUndo.set(key, entry);
  clearTimeout(el._toastTimer);
  el.classList.remove('toast-success', 'toast-error', 'toast-info');
  el.classList.add('toast-undo');
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.textContent = '';
  el.appendChild(document.createTextNode(_tr(message)));
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'toast-undo-btn';
  btn.textContent = window.BibliobrainI18n?.t?.('ui.undo') || 'Annuler';
  btn.onclick = () => {
    clearTimeout(entry.timer);
    _pendingUndo.delete(key);
    el.classList.remove('show', 'toast-undo');
    try { restore(); } catch (e) { /* l'état sera resynchronisé au prochain chargement */ }
  };
  el.appendChild(btn);
  void el.offsetWidth;
  el.classList.add('show');
  entry.timer = setTimeout(() => {
    _pendingUndo.delete(key);
    el.classList.remove('show', 'toast-undo');
    commit();
  }, duration);
  el._toastTimer = entry.timer;
}

// Si la page se ferme avec une suppression en attente, on la valide
// (fetch keepalive pour survivre au déchargement de la page).
window.addEventListener('pagehide', () => flushPendingUndo());
