// ---------------------------------------------
//  JSON REQUESTS AND SSE
// ---------------------------------------------
async function fetchJsonOrThrow(url, options = {}, fallback = 'Requete impossible') {
  const fetchOptions = { ...(options || {}) };
  const timeoutMs = Number(fetchOptions.timeoutMs || 0);
  delete fetchOptions.timeoutMs;
  let timeoutId = null;
  if (timeoutMs > 0 && !fetchOptions.signal) {
    const controller = new AbortController();
    fetchOptions.signal = controller.signal;
    timeoutId = window.setTimeout(() => controller.abort(), timeoutMs);
  }
  let response;
  try {
    response = await fetch(url, fetchOptions);
  } catch (e) {
    if (e?.name === 'AbortError') {
      throw new Error(`${fallback} : délai dépassé.`);
    }
    throw e;
  } finally {
    if (timeoutId) window.clearTimeout(timeoutId);
  }
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, fallback));
  }
  try {
    return await response.json() || {};
  } catch (_e) {
    return {};
  }
}

function postJsonOrThrow(url, payload, fallback = 'Requete impossible') {
  return fetchJsonOrThrow(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }, fallback);
}

async function fetchAllPaginated(url, keys, options = {}) {
  const pageSize = Math.max(1, Math.min(Number(options.limit || 250), 1000));
  const collected = Object.fromEntries(keys.map((key) => [key, []]));
  let offset = 0;
  let payload = null;
  for (let page = 0; page < 100; page += 1) {
    const separator = String(url).includes('?') ? '&' : '?';
    const pagePayload = await fetchJsonOrThrow(
      `${url}${separator}limit=${pageSize}&offset=${offset}`,
      options.fetchOptions || {},
      options.fallback || 'Chargement paginé impossible',
    );
    if (payload === null) payload = pagePayload;
    keys.forEach((key) => {
      const values = Array.isArray(pagePayload?.[key]) ? pagePayload[key] : [];
      collected[key] = options.prepend
        ? values.concat(collected[key])
        : collected[key].concat(values);
    });
    const pagination = pagePayload?.pagination;
    if (!pagination?.has_more) break;
    const received = Math.max(
      1,
      ...keys.map((key) => (Array.isArray(pagePayload?.[key]) ? pagePayload[key].length : 0)),
    );
    offset += received;
  }
  return { ...(payload || {}), ...collected };
}

async function responseErrorMessage(response, fallback = 'Requete impossible') {
  let detail = '';
  try {
    const raw = await response.text();
    if (raw) {
      try {
        const data = JSON.parse(raw);
        detail = data?.detail || data?.msg || data?.error || raw;
      } catch (_e) {
        detail = raw;
      }
    }
  } catch (_e) {}
  detail ||= response.statusText;
  const suffix = detail ? ` : ${String(detail).slice(0, 260)}` : '';
  return `${fallback} (${response.status})${suffix}`;
}

async function explainChatStreamError(error) {
  const raw = String(error?.message || error || 'Erreur reseau').trim();
  if (!/load failed|failed to fetch|networkerror|network request failed/i.test(raw)) {
    return raw;
  }
  try {
    const probe = await fetch('/api/startup-status', {
      method: 'GET',
      cache: 'no-store',
      headers: {'Accept': 'application/json'},
    });
    if (probe.ok) {
      return "Le backend répond, mais le flux de réponse du chat a été coupé par le navigateur mobile. Réessaie en gardant la page ouverte sur le même Wi-Fi; si ça revient, réduis temporairement la longueur de réponse ou teste un autre navigateur mobile.";
    }
    return `Le telephone atteint le backend, mais celui-ci repond avec une erreur (${probe.status}). Consulte les logs du backend sur le PC.`;
  } catch (_probeError) {
    return "Le telephone n'arrive plus a joindre le backend. Verifie que le PC serveur est allume, que l'adresse ouverte sur le telephone est bien http://IP_DU_PC:7860, que les deux appareils sont sur le meme reseau, et que le pare-feu autorise le port.";
  }
}

async function* readSSE(response) {
  if (!response) throw new Error('Aucune réponse du backend.');
  if (!response.ok) throw new Error(await responseErrorMessage(response, 'Chat impossible'));
  if (!response.body || typeof response.body.getReader !== 'function') {
    throw new Error('Le navigateur ne permet pas de lire le flux de réponse du chat.');
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, {stream: true});
    const parts = buffer.split('\n\n');
    buffer = parts.pop();
    for (const part of parts)
      for (const line of part.split('\n'))
        if (line.startsWith('data: '))
          try { yield JSON.parse(line.slice(6)); } catch(e) {}
  }
}
