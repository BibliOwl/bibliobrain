// Apply theme and performance preferences before the first paint.
(function applyEarlyPreferences() {
  try {
    const pref = localStorage.getItem('iassistant_theme') || 'dark';
    const light = pref === 'light'
      || (pref === 'auto' && window.matchMedia('(prefers-color-scheme: light)').matches);
    if (light) document.documentElement.setAttribute('data-theme', 'light');

    let performancePref = localStorage.getItem('iassistant_performance-mode') || 'auto';
    if (!['auto', 'normal', 'light'].includes(performancePref)) performancePref = 'auto';
    const cpuCount = Number(navigator.hardwareConcurrency || 0);
    const memoryGb = Number(navigator.deviceMemory || 0);
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const saveData = navigator.connection && navigator.connection.saveData === true;
    const autoLight = reducedMotion || saveData
      || (cpuCount > 0 && cpuCount <= 4)
      || (memoryGb > 0 && memoryGb <= 4);
    const useLightPerformance = performancePref === 'light'
      || (performancePref === 'auto' && autoLight);
    document.documentElement.setAttribute('data-performance-preference', performancePref);
    document.documentElement.setAttribute(
      'data-performance-mode',
      useLightPerformance ? 'light' : 'normal',
    );
  } catch (_error) {
    // The dark theme remains the safe default.
  }
})();
