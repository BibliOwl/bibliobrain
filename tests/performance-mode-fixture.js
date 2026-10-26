// Local preview of the production DOM and rendering code, with demonstration data.
window.addEventListener('load', async () => {
  const scripts = [
    'event-bindings.js', 'i18n.js', 'app.js', 'app/assistant-rendering.js', 'app/chat.js', 'app/discovery.js',
    'app/workspaces.js', 'app/library.js',
    'app/drafting.js', 'app/notebooks.js', 'settings.js', 'app/citation-graph.js',
    'knowledge-graph.js',
  ];
  for (const name of scripts) {
    await new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = '/static/js/' + name;
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
  }
  clearInterval(_gpuStatusRefreshTimer);
  document.getElementById('startup-boot-splash').hidden = true;
  document.getElementById('startup-help-modal').style.display = 'none';
  document.documentElement.dataset.theme = 'dark';
  state.appSettings = {};
  state.providerInfo = {};
  state.startupStatus = { library_configured: true, rag: { ready: true } };
  applyPerformanceModePreference();
  document.querySelectorAll('.nav-item[data-tab]').forEach(button => {
    button.addEventListener('click', () => {
      const name = button.dataset.tab;
      document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.toggle('active', panel.id === 'tab-' + name));
      document.querySelectorAll('.nav-item[data-tab]').forEach(nav => nav.classList.toggle('active', nav === button));
      window.dispatchEvent(new CustomEvent('bibliobrain:tabchange', { detail: { name } }));
    });
  });
  const source = { title: 'Restauration des zones humides et biodiversité', file: 'zones-humides.pdf', ref_label: 'S1', authors: ['Martin, Alice'], year: 2024, page: 12 };
  const answer = '## Les résultats principaux\n\nLa restauration des zones humides favorise le retour de la biodiversité. Les bénéfices dépendent du contexte local et de la durée du suivi [S1].\n\n### Trois points à retenir\n\n- La diversité des habitats soutient le retour des espèces.\n- La qualité de l’eau influence les résultats.\n- Un suivi sur plusieurs années permet de vérifier leur stabilité.\n\nCes observations invitent à associer **restauration écologique** et **suivi à long terme**.';
  const messages = document.getElementById('messages');
  messages.replaceChildren();
  const user = document.createElement('div');
  user.className = 'msg-wrap user';
  user.innerHTML = '<div class="avatar user">V</div><div class="bubble user">Quels sont les effets de la restauration des zones humides sur la biodiversité ?</div>';
  const bot = document.createElement('div');
  bot.className = 'msg-wrap bot';
  bot.innerHTML = '<div class="avatar bot">B</div><div class="bubble bot" id="performance-demo-answer"></div>';
  messages.append(user, bot);
  const bubble = document.getElementById('performance-demo-answer');
  window.performanceDemo = { bubble, answer, source };
  renderRichAssistantOutput(bubble, answer, { sources: [source], normalizeCitations: true });
  document.getElementById('model-select').innerHTML = '<option>Modèle de démonstration</option>';
  renderModelChip();
  ragStatusReady();
  const toolbar = document.createElement('aside');
  toolbar.setAttribute('aria-label', 'Aperçu avec données de démonstration');
  toolbar.style.cssText = 'position:fixed;right:18px;bottom:16px;z-index:10080;display:flex;gap:10px;align-items:center;padding:10px 14px;background:var(--surface);border:1px solid var(--border-2);border-radius:10px;font:12px Inter,system-ui;color:var(--text-2)';
  toolbar.innerHTML = '<span>Aperçu · données de démonstration</span><select aria-label="Profil de l’aperçu"><option value="light">Mode léger</option><option value="normal">Mode normal</option></select><button type="button" class="btn btn-ghost">Relancer la réponse</button>';
  toolbar.querySelector('select').value = document.documentElement.dataset.performanceMode;
  toolbar.querySelector('select').addEventListener('change', event => setPerformanceMode(event.target.value));
  let timer;
  toolbar.querySelector('button').addEventListener('click', () => {
    clearInterval(timer);
    let length = 0;
    bubble.replaceChildren();
    timer = setInterval(() => {
      length += 28;
      renderRichAssistantOutput(bubble, answer.slice(0, length), { streaming: length < answer.length, sources: [source], normalizeCitations: true });
      if (length >= answer.length) clearInterval(timer);
    }, 150);
  });
  document.body.appendChild(toolbar);
  document.body.dataset.performanceFixtureReady = 'true';
});
