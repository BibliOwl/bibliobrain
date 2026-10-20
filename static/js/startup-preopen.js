(function preopenStartupGuide() {
  try {
    if (localStorage.getItem('iassistant_startup-assistant-help-dismissed-v2') === '1') return;
  } catch (_error) {
    // Continue with the first-run guide when storage is unavailable.
  }
  const overlay = document.getElementById('startup-help-overlay');
  const modal = document.getElementById('startup-help-modal');
  if (!overlay || !modal) return;
  overlay.setAttribute('aria-hidden', 'false');
  overlay.classList.add('open');
  modal.classList.add('open');
  document.body.classList.add('startup-help-open');
  const splash = document.getElementById('startup-boot-splash');
  if (splash) splash.hidden = true;
})();
