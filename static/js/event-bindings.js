(function installBibliobrainEventBindings(global) {
  const handlers = new Map();
  let nextHandlerId = 1;
  const supportedEvents = [
    'click', 'change', 'input', 'keydown', 'keyup', 'submit', 'dragstart',
    'dragover', 'drop', 'contextmenu', 'dblclick', 'pointerdown', 'mousedown',
    'mouseover', 'mouseout', 'focus', 'blur',
  ];

  function attributeName(eventName) {
    return `data-bb-on${String(eventName || '').toLowerCase()}`;
  }

  function register(eventName, handler) {
    const normalizedEvent = String(eventName || '').toLowerCase();
    if (!supportedEvents.includes(normalizedEvent) || typeof handler !== 'function') {
      throw new TypeError('invalid_event_binding');
    }
    const id = `bbh-${nextHandlerId++}`;
    handlers.set(id, { eventName: normalizedEvent, handler });
    return { id, eventName: normalizedEvent };
  }

  global.bindBbHandler = function bindBbHandler(eventName, handler) {
    const binding = register(eventName, handler);
    return `${attributeName(binding.eventName)}="${binding.id}"`;
  };

  global.bindBbElement = function bindBbElement(element, eventName, handler) {
    if (!(element instanceof Element)) return '';
    const binding = register(eventName, handler);
    element.setAttribute(attributeName(binding.eventName), binding.id);
    return binding.id;
  };

  for (const eventName of supportedEvents) {
    document.addEventListener(eventName, (event) => {
      const origin = event.target instanceof Element ? event.target : null;
      const element = origin?.closest(`[${attributeName(eventName)}]`);
      if (!element) return;
      const id = element.getAttribute(attributeName(eventName));
      const binding = handlers.get(String(id || ''));
      if (!binding || binding.eventName !== eventName) return;
      const result = binding.handler.call(element, event);
      if (result === false) {
        event.preventDefault();
        event.stopPropagation();
      }
    }, eventName === 'focus' || eventName === 'blur');
  }

  const cleanupObserver = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
      for (const node of mutation.removedNodes) {
        if (!(node instanceof Element)) continue;
        // Re-parenting reports the node as removed even though it is still in
        // the document (the cartography modal moves between its inline
        // workspace and the overlay). Its handlers must survive the move.
        if (node.isConnected) continue;
        const removed = [node, ...node.querySelectorAll('*')];
        for (const element of removed) {
          for (const eventName of supportedEvents) {
            const id = element.getAttribute(attributeName(eventName));
            if (id) handlers.delete(id);
          }
        }
      }
    }
  });
  cleanupObserver.observe(document.documentElement, { childList: true, subtree: true });
})(window);
