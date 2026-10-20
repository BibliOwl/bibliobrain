// Run with Node and Playwright already available in NODE_PATH. No live backend is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');

async function main() {
  const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    if (pathname === '/') {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(fs.readFileSync(path.join(root, 'static/index.html'), 'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, ''));
    } else if (pathname.startsWith('/static/')) {
      const target = path.resolve(root, '.' + pathname);
      if (!target.startsWith(root + path.sep) || !fs.existsSync(target)) { res.writeHead(404).end(); return; }
      res.setHeader('Content-Type', pathname.endsWith('.css') ? 'text/css' : 'text/javascript');
      res.end(fs.readFileSync(target));
    } else res.writeHead(404).end();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let mode = 'ollama';
    let ollamaUrl = 'http://localhost:11434';
    let lmstudioUrl = 'http://127.0.0.1:1234';
    let embeddingSource = 'local';
    let toolsSource = 'local';
    let ocrReading = false;
    let remoteConfigured = false;
    let failSave = false;
    let failSwitch = false;
    let modelsDelay = 0;
    let modelsUnavailable = false;
    let promptText = 'Summarise {text}';
    let ocrSettings = { enabled: false, model: 'ovisocr2-benchmark:q8_0' };
    let ocrStatus = { active: false, status: 'idle', errors: [] };
    let failOcrSave = false;
    let ocrReadiness = { installed: true, label: 'Disponible', detail: 'Matériel de test' };
    let ocrInventory = { status: 'idle', active: false };
    let ocrInstall = { status: 200, payload: { ok: true } };
    let ocrStart = { status: 200, payload: { ok: true } };
    const ocrCalls = [];
    const writes = [];
    const info = () => {
      // Like the server: the index stays on this computer unless it follows the chat.
      const followsChat = embeddingSource === 'chat';
      const viaLmStudio = followsChat && mode === 'lmstudio';
      const localOllama = ollamaUrl.includes('localhost') ? ollamaUrl : 'http://localhost:11434';
      return {
        mode, effective_mode: mode, configured: true, can_use_remote: remoteConfigured,
        ollama_base_url: ollamaUrl, lmstudio_base_url: lmstudioUrl,
        provider_url: mode === 'remote' ? 'https://example.test/v1' : '',
        embedding_provider: viaLmStudio ? 'lmstudio' : 'ollama', local_embeddings_required: true,
        embedding_source: embeddingSource,
        embedding_base_url: viaLmStudio ? lmstudioUrl : (followsChat ? ollamaUrl : localOllama),
        document_tools_source: toolsSource,
        document_tools_base_url: toolsSource === 'chat' ? ollamaUrl : localOllama,
        required_embedding_model: 'embeddinggemma',
      };
    };
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const body = route.request().postDataJSON();
      let payload = { ok: true };
      let status = 200;
      if (route.request().method() !== 'GET') writes.push({ pathname: url.pathname, body });
      if (url.pathname === '/api/provider-info') payload = info();
      if (url.pathname === '/api/set-provider') {
        if (failSwitch) { status = 409; payload = { ok: false, msg: 'Remote error from old API wording' }; }
        else {
          if (body.url && body.mode === 'lmstudio') lmstudioUrl = body.url;
          if (body.url && body.mode === 'ollama') ollamaUrl = body.url;
          mode = body.mode;
          payload = { ok: true, effective_mode: mode, applied: true, info: info() };
        }
      }
      if (url.pathname === '/api/set-document-tools-source') {
        if (ocrReading) { status = 409; payload = { ok: false, code: 'ocr_active' }; }
        else { toolsSource = body.source; payload = { ok: true, source: toolsSource, info: info() }; }
      }
      if (url.pathname === '/api/set-embedding-source') {
        embeddingSource = body.source;
        payload = { ok: true, source: embeddingSource, index_reused: false, info: info() };
      }
      if (url.pathname === '/api/models') {
        const requestMode = mode;
        const delay = modelsDelay;
        if (delay) await new Promise(resolve => setTimeout(resolve, delay));
        payload = { ok: true, models: modelsUnavailable ? [] : [requestMode + '-chat-model'], health: { required: true, ok: false, ollama_ok: false, msg: 'Indexing service unavailable' } };
      }
      if (url.pathname === '/api/embedding-models') payload = { ok: true, models: ['embeddinggemma'], required_embedding_model: 'embeddinggemma' };
      if (url.pathname === '/api/api-secrets') {
        if (body?.remote_llm_base_url) remoteConfigured = true;
        payload = { ok: true, available: true, remote_llm_base_url: { value: remoteConfigured ? 'https://example.test/v1' : '', configured: remoteConfigured }, secrets: {} };
      }
      if (url.pathname === '/api/app-settings' || url.pathname === '/api/rag-settings') {
        status = failSave ? 500 : 200;
        payload = failSave ? { ok: false, msg: 'Save failed for test' } : { ok: true, ...body };
      }
      if (url.pathname === '/api/analysis-prompts') {
        if (body?.prompt) promptText = body.prompt;
        payload = { ok: true, prompts: [
          { type: 'summary', label: 'Résumé', prompt: promptText, default_prompt: 'Summarise {text}' },
          { type: 'methods', label: 'Méthodes', prompt: 'Methods {text}', default_prompt: 'Methods {text}' },
        ] };
      }
      if (url.pathname === '/api/ocr/settings') {
        if (failOcrSave) { status = 500; payload = { ok: false, msg: 'OCR save failed for test' }; }
        else ocrSettings = body;
      }
      if (url.pathname === '/api/ocr/status') payload = { settings: ocrSettings, ...ocrStatus };
      if (url.pathname.startsWith('/api/ocr/') && url.pathname !== '/api/ocr/status') ocrCalls.push(url.pathname);
      if (url.pathname === '/api/ocr/readiness') payload = ocrReadiness;
      if (url.pathname === '/api/ocr/inventory') payload = route.request().method() === 'GET' ? ocrInventory : { ok: true };
      if (url.pathname === '/api/ocr/install') ({ status, payload } = ocrInstall);
      if (url.pathname === '/api/ocr/start') {
        ({ status, payload } = ocrStart);
        if (payload.ok) ocrStatus = { active: true, status: 'running', candidates: 5, completed: 1, errors: [] };
      }
      await route.fulfill({ status, json: payload });
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.evaluate(() => {
      document.getElementById('startup-boot-splash')?.remove();
      window.state = { appSettings: {}, providerInfo: {}, startupStatus: { library_configured: true, rag: { ready: false } }, chatSessions: [] };
      window.escapeHtml = text => String(text).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
      window.escapeAttr = window.escapeHtml;
      window.showToast = () => {};
      window._showSettingSaved = () => {};
      window.getVectorDbEmbeddingWarning = () => '';
      window.fetchStartupStatus = async () => state.startupStatus;
      window.renderStartupStatus = () => refreshSettingsOverview();
      window.renderOllamaStatus = () => {};
      window.postJsonOrThrow = async (url, body) => {
        const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const d = await r.json(); if (!r.ok) throw new Error(d.msg); return d;
      };
      window.inferMetadataSource = () => 'none';
      window.updatePdfMetadataEnrichmentUi = () => {};
      window.updateChatVisualForceUi = () => {};
      window.openStartupAssistant = () => {};
      window.switchTab = name => { window.openedTab = name; };
    });
    for (const script of ['event-bindings.js', 'i18n.js', 'app/settings-panel.js', 'settings.js', 'static-event-bindings.js']) {
      await page.addScriptTag({ url: `/static/js/${script}` });
    }
    await page.evaluate(async () => {
      clearInterval(_gpuStatusRefreshTimer);
      // Bind production handlers without starting the rest of the research workspace.
      _hookPersistence();
      document.getElementById('remote-connection-save').addEventListener('click', async () => {
        const saved = await saveApiSecrets('remote'); if (saved) await setProviderMode('remote');
      });
      document.getElementById('ollama-url').addEventListener('change', () => syncOllamaBaseUrl({ silent: true }));
      await loadProviderInfo();
      toggleSettings();
      switchSettingsTab('models');
    });

    assert.equal(await page.locator('#settings-remote-connection').isVisible(), false);
    assert.equal(await page.locator('#settings-local-connection').isVisible(), true);
    const checked = location => page.getAttribute(`[data-provider-location=${location}]`, 'aria-checked');
    const pressed = software => page.getAttribute(`[data-provider-software=${software}]`, 'aria-pressed');
    const providerWrites = () => writes.filter(write => write.pathname === '/api/set-provider');
    assert.equal(await checked('local'), 'true', 'Ollama on localhost is "this computer"');
    assert.equal(await pressed('ollama'), 'true');
    assert.equal(
      await page.locator('#document-tools-source-select option[value=chat]').isDisabled(), true,
      'No other Ollama is known yet: scans and figures can only be read here',
    );
    await page.click('[data-provider-location=remote]');
    await page.waitForFunction(() => document.getElementById('provider-mode-status').textContent.includes('Renseignez'));
    assert.equal(mode, 'ollama', 'Unconfigured remote choice must not change the active service');
    assert.equal(await page.locator('#settings-local-connection').isVisible(), false);
    assert.equal(await page.locator('#settings-local-software').isVisible(), false, 'Ollama / LM Studio is not asked for an online API');
    assert.equal(await page.locator('#compute-mode-verification-row').isVisible(), false);
    await page.click('[data-provider-location=local]');
    await page.waitForFunction(() => !document.getElementById('compute-mode-verification-row').hidden);
    assert.equal(providerWrites().length, 0, 'Coming back to the active service writes nothing');
    assert.equal(await page.locator('#provider-mode-status').isVisible(), false, 'Previous remote error must disappear');

    // The software is chosen independently of where it runs.
    await page.click('[data-provider-software=lmstudio]');
    await page.waitForFunction(() => document.getElementById('compute-mode-status').textContent.includes('LM Studio connecté'));
    assert.deepEqual(providerWrites().at(-1).body, { mode: 'lmstudio', url: 'http://127.0.0.1:1234' });
    assert.equal(await checked('local'), 'true');
    assert.equal(await pressed('lmstudio'), 'true');
    assert.equal(await page.inputValue('#ollama-url'), 'http://127.0.0.1:1234');
    assert.match(await page.textContent('#settings-active-provider'), /LM Studio, sur cet ordinateur/);
    assert.equal(await page.inputValue('#settings-model-select'), 'lmstudio-chat-model');
    fs.mkdirSync(path.join(root, 'tests_tmp/settings-preview'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'tests_tmp/settings-preview/lmstudio.png') });
    modelsUnavailable = true;
    await page.evaluate(() => verifyComputeSettings());
    assert.match(await page.textContent('#compute-mode-status'), /Aucun modèle accessible sur LM Studio/);
    assert.equal(await page.locator('#settings-model-select').isDisabled(), true);
    modelsUnavailable = false;
    await page.evaluate(() => verifyComputeSettings());

    // Failed mutations do not invent an active mode or reuse a remote API error message.
    failSwitch = true;
    await page.click('[data-provider-software=ollama]');
    await page.waitForFunction(() => document.getElementById('provider-mode-status').textContent.includes('Impossible'));
    assert.equal(mode, 'lmstudio');
    assert.match(await page.textContent('#settings-active-provider'), /LM Studio/);
    assert.doesNotMatch(await page.textContent('#provider-mode-status'), /Remote error/);
    failSwitch = false;
    const switchWrites = providerWrites().length;
    await page.click('[data-provider-software=lmstudio]');
    await page.waitForFunction(() => !document.getElementById('provider-mode-select').disabled);
    assert.equal(providerWrites().length, switchWrites, 'Returning to the active service does not reset the index');

    // An older delayed response must never overwrite a newer provider's models.
    modelsDelay = 450;
    await page.evaluate(() => { window.oldModels = loadModels({ silent: true }); });
    await page.waitForTimeout(30);
    modelsDelay = 0;
    await page.click('[data-provider-software=ollama]');
    await page.waitForFunction(() => document.getElementById('settings-model-select').value === 'ollama-chat-model');
    await page.evaluate(() => window.oldModels);
    assert.equal(await page.inputValue('#settings-model-select'), 'ollama-chat-model');

    // Another computer: nothing is applied until its address is known.
    const beforeNetwork = providerWrites().length;
    await page.click('[data-provider-location=network]');
    await page.waitForFunction(() => document.getElementById('provider-mode-status').textContent.includes('Ollama fonctionne'));
    assert.equal(providerWrites().length, beforeNetwork);
    assert.equal(await checked('network'), 'true');
    assert.equal(await page.inputValue('#ollama-url'), '');
    assert.equal(await page.locator('#ollama-default-url-btn').isVisible(), false);
    assert.equal(await page.locator('#compute-mode-verification-row').isVisible(), false);
    assert.match(await page.textContent('#provider-url-desc'), /OLLAMA_HOST/);
    await page.evaluate(() => { document.activeElement.blur(); return loadProviderInfo(); });
    assert.equal(await page.inputValue('#ollama-url'), '', 'A background refresh keeps the pending address empty');
    await page.fill('#ollama-url', 'http://192.168.1.20:11434');
    await page.locator('#ollama-url').blur();
    await page.waitForFunction(() => document.getElementById('compute-mode-status').textContent.includes('Ollama connecté'));
    assert.deepEqual(providerWrites().at(-1).body, { mode: 'ollama', url: 'http://192.168.1.20:11434' });
    assert.equal(await checked('network'), 'true');
    assert.match(await page.textContent('#settings-active-provider'), /Ollama, sur un autre ordinateur/);
    assert.match(await page.textContent('#settings-overview-assistant'), /autre ordinateur · Ollama/);

    // Scans and figures stay on this computer; Expert can send them to the other Ollama.
    const toolsWrites = () => writes.filter(write => write.pathname === '/api/set-document-tools-source');
    const otherOllama = page.locator('#document-tools-source-select option[value=chat]');
    assert.equal(await page.inputValue('#document-tools-source-select'), 'local');
    assert.equal(await otherOllama.isDisabled(), false);
    assert.ok((await otherOllama.textContent()).includes('http://192.168.1.20:11434'));
    await page.evaluate(() => setDocumentToolsSource('chat'));
    assert.deepEqual(toolsWrites().at(-1).body, { source: 'chat' });
    assert.equal(await page.inputValue('#document-tools-source-select'), 'chat');
    assert.ok((await page.textContent('#document-tools-source-status')).includes('http://192.168.1.20:11434'));
    ocrReading = true;
    await page.evaluate(() => setDocumentToolsSource('local'));
    assert.equal(await page.inputValue('#document-tools-source-select'), 'chat', 'Not changed while scanned pages are being read');
    assert.match(await page.textContent('#document-tools-source-status'), /en pause/);
    ocrReading = false;
    await page.evaluate(() => setDocumentToolsSource('local'));
    assert.equal(await page.inputValue('#document-tools-source-select'), 'local');
    assert.match(await page.textContent('#document-tools-source-status'), /sur cet ordinateur/);
    await page.waitForTimeout(250); // let the card transition settle for the preview
    await page.screenshot({ path: path.join(root, 'tests_tmp/settings-preview/network-ollama.png') });

    // LM Studio on the other computer has its own address; the pasted "/v1" is dropped.
    await page.click('[data-provider-software=lmstudio]');
    await page.waitForFunction(() => document.getElementById('provider-mode-status').textContent.includes('LM Studio fonctionne'));
    assert.equal(mode, 'ollama');
    assert.match(await page.textContent('#provider-url-desc'), /Serve on Local Network/);
    await page.fill('#ollama-url', 'http://192.168.1.20:1234/v1');
    await page.locator('#ollama-url').blur();
    await page.waitForFunction(() => document.getElementById('compute-mode-status').textContent.includes('LM Studio connecté'));
    assert.deepEqual(providerWrites().at(-1).body, { mode: 'lmstudio', url: 'http://192.168.1.20:1234' });
    assert.equal(ollamaUrl, 'http://192.168.1.20:11434', 'Switching software leaves the other address alone');

    // The index stays with Ollama on this computer until Expert says otherwise.
    const sourceWrites = () => writes.filter(write => write.pathname === '/api/set-embedding-source');
    assert.match(await page.textContent('#settings-indexing-provider'), /Ollama, sur cet ordinateur/);
    assert.equal(await page.inputValue('#embedding-source-select'), 'local');
    await page.evaluate(() => setEmbeddingSource('chat'));
    assert.deepEqual(sourceWrites().at(-1).body, { source: 'chat' });
    assert.equal(await page.inputValue('#embedding-source-select'), 'chat');
    assert.ok((await page.textContent('#settings-indexing-provider')).includes('LM Studio, sur un autre ordinateur (http://192.168.1.20:1234)'));
    assert.match(await page.textContent('#embedding-source-status'), /Un index sera construit/);
    const embeddingReads = [];
    page.on('request', request => { if (request.url().includes('/api/ollama-')) embeddingReads.push(request.url()); });
    await page.evaluate(() => setEmbeddingSource('local'));
    assert.equal(sourceWrites().length, 2);
    assert.match(await page.textContent('#settings-indexing-provider'), /Ollama, sur cet ordinateur/);
    assert.ok(embeddingReads.length > 0 && embeddingReads.every(read => !read.includes('url=')), 'Index checks never send the chat address');
    await page.evaluate(() => setEmbeddingSource('local'));
    assert.equal(sourceWrites().length, 2, 'Choosing the active source again writes nothing');

    // Back to this computer: the software stays, the address returns to its default.
    await page.click('[data-provider-location=local]');
    await page.waitForFunction(() => document.getElementById('compute-mode-status').textContent.includes('LM Studio connecté'));
    assert.deepEqual(providerWrites().at(-1).body, { mode: 'lmstudio', url: 'http://127.0.0.1:1234' });
    assert.match(await page.textContent('#settings-active-provider'), /LM Studio, sur cet ordinateur/);
    await page.click('[data-provider-software=ollama]');
    await page.waitForFunction(() => document.getElementById('settings-model-select').value === 'ollama-chat-model');
    assert.deepEqual(providerWrites().at(-1).body, { mode: 'ollama', url: 'http://localhost:11434' });
    assert.equal(await checked('local'), 'true');

    await page.click('[data-provider-location=remote]');
    await page.fill('#remote-llm-base-url', 'https://example.test/v1');
    await page.fill('#remote-llm-api-key', 'fake-test-value');
    await page.evaluate(() => { closeSettings(); toggleSettings(); switchSettingsTab('models'); });
    assert.equal(await page.inputValue('#remote-llm-api-key'), 'fake-test-value', 'Closing the panel keeps the unsaved draft');
    await page.click('#remote-connection-save');
    await page.waitForFunction(() => document.getElementById('compute-mode-status').textContent.includes('connecté'));
    assert.equal(mode, 'remote');
    assert.equal(await page.inputValue('#remote-llm-api-key'), '');
    assert.equal(await page.locator('#settings-local-connection').isVisible(), false);
    assert.match(await page.textContent('#compute-mode-status'), /connecté/);
    assert.equal(await page.locator('#llm-thinking').isVisible(), false, 'Ollama-only reasoning is not shown for remote services');

    // A search looks through Expert too and shows both navigations.
    await page.evaluate(() => { switchSettingsTab('general'); filterSettings('température'); });
    assert.equal(await page.locator('#llm-temperature').isVisible(), true);
    assert.equal(await page.getAttribute('#settings-panel', 'data-settings-mode'), 'expert');
    assert.equal(await page.locator('.settings-nav-label[data-settings-mode=everyday]').isVisible(), true);
    await page.evaluate(() => filterSettings(''));
    assert.equal(await page.locator('.settings-nav-label[data-settings-mode=everyday]').isVisible(), false);
    assert.equal(await page.locator('[data-settings-tab=documents]').isVisible(), false);
    assert.equal(await page.locator('#settings-expert-banner').isVisible(), true);

    const generationSection = page.locator('#llm-temperature').locator('xpath=ancestor::div[contains(concat(" ", @class, " "), " settings-section ")][1]');
    const temperatureRow = page.locator('#llm-temperature').locator('xpath=ancestor::div[contains(concat(" ", @class, " "), " settings-row ")][1]');
    failSave = true;
    await page.evaluate(async () => {
      const input = document.getElementById('llm-temperature');
      input.value = '0.3';
      await syncAppSettings({ silent: true, source: input });
    });
    assert.match(await generationSection.textContent(), /Échec/);
    failSave = false;
    await page.locator('#settings-panel').getByRole('button', { name: 'Réessayer', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.settings-save-feedback[data-state=saved]'));
    assert.match(await page.textContent('#settings-generation-summary'), /personnalisés/);
    // Each tuned value shows its default and resets alone.
    assert.match(await temperatureRow.textContent(), /Par défaut : 0\.10/);
    const temperatureReset = temperatureRow.locator('.settings-reset-one');
    assert.equal(await temperatureReset.isDisabled(), false);
    // Saving stays with the production change handlers (bound on DOMContentLoaded, not here).
    await page.evaluate(() => {
      window.temperatureChanges = 0;
      document.getElementById('llm-temperature').addEventListener('change', () => { window.temperatureChanges += 1; });
    });
    await temperatureReset.click();
    await page.waitForFunction(() => document.getElementById('llm-temperature').value === '0.1');
    assert.equal(await page.textContent('#llm-temperature-val'), '0.10');
    assert.equal(await temperatureReset.isDisabled(), true);
    assert.equal(await page.evaluate(() => window.temperatureChanges), 1);

    await page.evaluate(() => { state.appSettings.first_rag_init_done = true; switchSettingsTab('research'); closeSettings(); toggleSettings(); });
    assert.equal(await page.locator('#settings-pane-online').isVisible(), true, 'The former research tab opens Finding articles online');
    await page.click('#settings-semantic-key-btn');
    await page.waitForFunction(() => document.activeElement?.id === 'semantic-scholar-api-key');
    assert.equal(await page.getAttribute('#settings-panel', 'data-settings-mode'), 'expert');
    await page.evaluate(() => { switchSettingsTab('preparation'); updateVisualAnalysisUi(); });
    assert.equal(await page.locator('#vision-model-select').isVisible(), false);

    // Exercise the production explicit-save editors with isolated backend fixtures.
    await page.addScriptTag({ url: '/static/js/app/library.js' });
    await page.evaluate(async () => {
      window.fetchJsonOrThrow = async url => (await fetch(url)).json();
      window.loadLibrary = async () => {};
      await loadAnalysisPrompts();
      switchSettingsTab('prompts');
    });
    await page.selectOption('#analysis-prompt-type-select', 'methods');
    assert.equal(await page.inputValue('#analysis-prompt-editor'), 'Methods {text}');
    assert.equal(await page.locator('#analysis-prompt-type-select').isDisabled(), false);
    await page.selectOption('#analysis-prompt-type-select', 'summary');
    await page.fill('#analysis-prompt-editor', 'My instructions {text}');
    assert.equal(await page.locator('#analysis-prompt-type-select').isDisabled(), true);
    await page.evaluate(async () => { closeSettings(); toggleSettings(); switchSettingsTab('prompts'); await loadAnalysisPrompts(); });
    assert.equal(await page.inputValue('#analysis-prompt-editor'), 'My instructions {text}');
    await page.evaluate(() => saveAnalysisPromptOverride());
    assert.equal(promptText, 'My instructions {text}');
    assert.equal(await page.locator('#analysis-prompt-type-select').isDisabled(), false);
    assert.equal(await page.evaluate(() => settingsDraftIsDirty('prompt')), false);
    await page.evaluate(() => switchSettingsTab('expert-indexing'));
    // An idle OCR queue is polled every 30 s; shorten only that wait so the
    // backend changes simulated below reach the page without sleeping.
    await page.evaluate(() => {
      const realSetTimeout = window.setTimeout.bind(window);
      window.setTimeout = (callback, delay, ...args) => realSetTimeout(callback, delay >= 30000 ? 200 : delay, ...args);
    });
    await page.addScriptTag({ url: '/static/js/app/ocr.js' });
    await page.waitForFunction(() => !document.getElementById('ocr-enabled').disabled);
    assert.equal(await page.locator('#ocr-start').isDisabled(), true);
    await page.check('#ocr-enabled');
    await page.click('#ocr-save');
    await page.waitForFunction(() => !settingsDraftIsDirty('ocr'));
    assert.equal(ocrSettings.enabled, true);
    assert.equal(await page.locator('#ocr-start').isDisabled(), false);
    await page.uncheck('#ocr-enabled');
    await page.click('#ocr-save');
    await page.waitForFunction(() => !settingsDraftIsDirty('ocr'));
    assert.equal(ocrSettings.enabled, false);
    assert.equal(writes.some(write => /\/api\/ocr\/(install|start)/.test(write.pathname)), false, 'Saving OCR preferences never installs or starts a model');
    ocrSettings.enabled = true;
    ocrStatus = { active: false, status: 'paused', candidates: 1535, completed: 12, ignored_non_pdf_errors: 230,
      errors: Array.from({ length: 63 }, (_, i) => ({ file: `ABC${i}\\${'long-paper-name-'.repeat(12)}.pdf`, page: i + 1, issue: 'PdfStreamError' })) };
    await page.waitForFunction(() => document.getElementById('ocr-errors-summary').textContent.includes('63'));
    assert.equal(await page.textContent('#ocr-start'), 'Reprendre');
    assert.equal(await page.locator('#ocr-pause').evaluate(el => el.hidden), true);
    // Failures stay listed on the My documents card while the pass is paused.
    await page.evaluate(() => switchSettingsTab('documents'));
    assert.equal(await page.getAttribute('#settings-scans', 'data-ocr-view'), 'working');
    assert.equal(await page.textContent('#ocr-quickfix-pause'), 'Reprendre');
    assert.equal(await page.locator('#ocr-errors li').count(), 20);
    assert.equal(await page.locator('#ocr-errors-wrap').getAttribute('open'), null);
    await page.locator('#ocr-errors-summary').click();
    assert.match(await page.locator('.ocr-error-message').first().textContent(), /PDF illisible/);
    await page.evaluate(() => window.BibliobrainI18n.setLanguage('en'));
    assert.equal(await page.textContent('#ocr-start'), 'Resume');
    assert.match(await page.textContent('#ocr-errors-summary'), /Items to review/);
    // Drafting panels hand i18n keys to _tr(); they must not render raw.
    assert.equal(
      await page.evaluate(() => window.BibliobrainI18n.translateRaw('drafting.argument.claimsTitle')),
      'Draft claims',
    );
    await page.evaluate(() => window.BibliobrainI18n.setLanguage('fr'));
    assert.equal(
      await page.evaluate(() => window.BibliobrainI18n.translateRaw('drafting.argument.claim.needsSource')),
      await page.evaluate(() => window.BibliobrainI18n.t('drafting.argument.claim.needsSource')),
    );
    assert.notEqual(
      await page.evaluate(() => window.BibliobrainI18n.translateRaw('drafting.argument.claim.needsSource')),
      'drafting.argument.claim.needsSource',
    );
    assert.equal(await page.locator('#ocr-retry').isDisabled(), false);
    await page.click('#ocr-errors-more');
    assert.equal(await page.locator('#ocr-errors li').count(), 40);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.locator('#settings-scans').evaluate(el => el.scrollWidth > el.clientWidth), false, 'OCR long paths and actions must wrap on mobile');
    assert.ok((await page.locator('#ocr-errors').boundingBox()).height <= 241, 'Error list must remain bounded');
    await page.locator('#ocr-errors-summary').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(root, 'tests_tmp/settings-preview/ocr-mobile.png') });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.evaluate(() => switchSettingsTab('expert-indexing'));
    await page.locator('#settings-ocr-expert').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(root, 'tests_tmp/settings-preview/ocr.png') });
    await page.fill('#ocr-model', 'draft-model');
    assert.equal(await page.locator('#ocr-start').isDisabled(), true, 'Do not start with unsaved settings');
    assert.equal(await page.locator('#ocr-retry').isDisabled(), true);
    failOcrSave = true;
    // The next status poll replaces the error text; record every text shown.
    await page.evaluate(() => {
      const status = document.getElementById('ocr-status');
      window.ocrStatusHistory = [];
      new MutationObserver(() => window.ocrStatusHistory.push(status.textContent))
        .observe(status, { childList: true, characterData: true, subtree: true });
    });
    await page.click('#ocr-save');
    await page.waitForFunction(() => window.ocrStatusHistory.some(text => text.includes('OCR save failed')));
    assert.equal(await page.evaluate(() => settingsDraftIsDirty('ocr')), true);
    assert.equal(await page.inputValue('#ocr-model'), 'draft-model');
    assert.equal(await page.locator('#ocr-start').isDisabled(), true);
    failOcrSave = false;
    await page.click('#ocr-save');
    await page.waitForFunction(() => !document.getElementById('ocr-start').disabled);
    ocrStatus = { ...ocrStatus, active: true, status: 'running' };
    await page.waitForFunction(() => !document.getElementById('ocr-pause').hidden);
    assert.equal(await page.locator('#ocr-start').isVisible(), false);
    assert.equal(await page.locator('#ocr-model').isDisabled(), true);
    assert.equal(await page.locator('#ocr-retry').isDisabled(), true);
    ocrStatus = { ...ocrStatus, status: 'pausing' };
    await page.waitForFunction(() => document.getElementById('ocr-pause').disabled);
    ocrStatus = { active: false, status: 'completed', errors: [] };
    await page.waitForFunction(() => document.getElementById('ocr-errors-wrap').hidden);

    // "Fix this": Overview → check → explanation card → Start chains readiness,
    // install, settings and start; each failure gets its own card, not a toast.
    ocrSettings = { enabled: false, model: 'ovisocr2-benchmark:q8_0' };
    ocrStatus = { active: false, status: 'idle', errors: [] };
    ocrReadiness = { installed: false, reason: 'model_missing', level: 'unknown', ram_bytes: 16 * 1024 ** 3 };
    ocrInventory = { status: 'completed', active: false, documents: 2, pages: 5, documents_total: 9, documents_scanned: 9 };
    await page.evaluate(async () => {
      state.startupStatus = { library_configured: true, library_count: 9, rag: { ready: true } };
      await window.refreshOcrStatus();
      switchSettingsTab('overview');
    });
    assert.equal(await page.textContent('#settings-overview-documents-action'), 'Vérifier');
    await page.click('#settings-overview-documents-action');
    await page.waitForFunction(() => document.getElementById('settings-scans').dataset.ocrView === 'offer');
    assert.equal(await page.locator('#settings-pane-documents').isVisible(), true);
    assert.match(await page.textContent('#ocr-quickfix-offer-text'), /^2 PDF/);
    assert.match(await page.textContent('#settings-documents-pill'), /1 point à corriger/);
    await page.evaluate(() => switchSettingsTab('overview'));
    assert.match(await page.textContent('#settings-overview-ready'), /3 sur 4/);
    assert.equal(await page.textContent('#settings-overview-documents-action'), 'Corriger');
    await page.click('#settings-overview-documents-action');
    await page.waitForFunction(() => document.getElementById('settings-scans').dataset.ocrView === 'confirm');
    assert.match(await page.textContent('#ocr-quickfix-step-read'), /5 pages/);
    assert.match(await page.textContent('#ocr-quickfix-memory'), /16 Go/);
    assert.equal(await page.locator('#ocr-quickfix-step-download').isVisible(), true);

    ocrInstall = { status: 503, payload: { ok: false, error: 'ollama_unavailable:connection refused' } };
    ocrCalls.length = 0;
    await page.click('#ocr-quickfix-start');
    await page.waitForFunction(() => document.getElementById('settings-scans').dataset.ocrView === 'blocked');
    assert.deepEqual(ocrCalls, ['/api/ocr/readiness', '/api/ocr/install']);
    assert.match(await page.textContent('#ocr-quickfix-blocked-title'), /ne peut pas démarrer/);
    assert.equal(await page.locator('#ocr-quickfix-blocked-steps li').count(), 2);
    assert.equal(await page.locator('#ocr-quickfix-blocked-link').isVisible(), false);

    ocrInstall = { status: 200, payload: { ok: true } };
    ocrStart = { status: 409, payload: { ok: false, code: 'index_busy', msg: 'Une indexation est en cours.' } };
    ocrCalls.length = 0;
    await page.click('#ocr-quickfix-retry');
    await page.waitForFunction(() => document.getElementById('ocr-quickfix-blocked-title').textContent.includes('mise à jour'));
    // A passive readiness refresh may follow once the model is installed.
    assert.deepEqual(ocrCalls.slice(0, 4), ['/api/ocr/readiness', '/api/ocr/install', '/api/ocr/settings', '/api/ocr/start']);
    assert.deepEqual(writes.filter(write => write.pathname === '/api/ocr/install').at(-1).body, { model: 'ovisocr2-benchmark:q8_0', confirmed: true });
    assert.equal(ocrSettings.enabled, true);

    ocrSettings = { enabled: false, model: 'ovisocr2-benchmark:q8_0' };
    ocrStart = { status: 409, payload: { ok: false, code: 'library_not_loaded', msg: 'Chargez d’abord la bibliothèque vectorisée.' } };
    await page.click('#ocr-quickfix-retry');
    await page.waitForFunction(() => document.getElementById('ocr-quickfix-blocked-title').textContent.includes('Chargez'));
    assert.equal(await page.locator('#ocr-quickfix-blocked-link').isVisible(), true);

    ocrStart = { status: 200, payload: { ok: true } };
    await page.click('#ocr-quickfix-retry');
    await page.waitForFunction(() => document.getElementById('settings-scans').dataset.ocrView === 'working');
    assert.match(await page.textContent('#ocr-quickfix-progress-text'), /page 1 sur 5/);
    assert.equal(await page.getAttribute('[data-ocr-step=download]', 'data-state'), 'done');
    assert.equal(await page.getAttribute('[data-ocr-step=read]', 'data-state'), 'active');
    assert.equal(await page.textContent('[data-area-badge=documents]'), '20%');
    await page.screenshot({ path: path.join(root, 'tests_tmp/settings-preview/fix-this.png') });
    ocrStatus = { active: false, status: 'completed_with_review', indexed_pages: 4, errors: [{ file: 'a/locked.pdf', page: null, issue: 'FileNotDecryptedError' }] };
    await page.evaluate(() => window.refreshOcrStatus());
    await page.waitForFunction(() => document.getElementById('settings-scans').dataset.ocrView === 'done');
    assert.match(await page.textContent('#ocr-quickfix-done-title'), /4 pages scannées/);
    assert.match(await page.textContent('#ocr-quickfix-issue'), /1 PDF n’a pas pu être lu entièrement\. PDF protégé/);
    await page.click('#ocr-quickfix-see');
    assert.equal(await page.locator('#ocr-errors-wrap').getAttribute('open'), '');
    assert.match(await page.textContent('#settings-documents-pill'), /Tout va bien/);

    // Expert is a separate door; each mode remembers its page.
    await page.evaluate(() => switchSettingsTab('overview'));
    await page.click('#settings-mode-toggle');
    assert.equal(await page.getAttribute('#settings-panel', 'data-settings-mode'), 'expert');
    assert.equal(await page.locator('#settings-expert-banner').isVisible(), true);
    assert.equal(await page.locator('[data-settings-tab=documents]').isVisible(), false);
    await page.click('#settings-mode-toggle');
    assert.equal(await page.locator('#settings-pane-overview').isVisible(), true);
    assert.equal(await page.locator('#settings-expert-banner').isVisible(), false);

    fs.mkdirSync(path.join(root, 'tests_tmp/settings-preview'), { recursive: true });
    await page.evaluate(() => switchSettingsTab('documents'));
    await page.screenshot({ path: path.join(root, 'tests_tmp/settings-preview/documents.png') });
    await page.evaluate(() => switchSettingsTab('models'));
    await page.screenshot({ path: path.join(root, 'tests_tmp/settings-preview/assistant.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(300);
    await page.screenshot({ path: path.join(root, 'tests_tmp/settings-preview/mobile.png') });
    const overflow = await page.locator('#settings-panel').evaluate(el => el.scrollWidth > el.clientWidth);
    assert.equal(overflow, false, 'Settings must fit a mobile viewport');
    const bounds = await page.locator('#settings-panel').boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390, 'Mobile dialog must remain inside the viewport');
    await page.locator('#settings-remote-options summary').click();
    assert.ok((await page.locator('#remote-llm-base-url').boundingBox()).width > 160, 'Mobile connection input must have usable width');
    await page.evaluate(() => window.BibliobrainI18n.setLanguage('en'));
    assert.equal(await page.locator('[data-settings-tab=assistant]').innerText(), 'AI assistant');
    await page.locator('#settings-search').focus();
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => {
      const active = document.activeElement;
      return document.getElementById('settings-panel').contains(active) && active.id !== 'settings-search';
    }), true, 'Keyboard focus remains inside the dialog');
    assert.equal(new Set(await page.locator('[id]').evaluateAll(els => els.map(el => el.id))).size, await page.locator('[id]').count(), 'No duplicate HTML IDs');
    assert.deepEqual(errors, []);
    console.log('PASS: provider cards, failed switch, stale response, remote setup, retained drafts, search across modes, failed save/retry, per-setting reset, last tab, prompt editor, OCR explicit save, Fix this flow and its blocked cards, Expert door, desktop/mobile, English, keyboard focus, unique IDs.');
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
