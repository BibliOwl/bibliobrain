// ---------------------------------------------
//  LEARNING THE APP
// ---------------------------------------------
// Three layers, lightest first:
// - the tutorial (startChatTutorial) has the user ask a real question and
//   open a citation; it only starts from "Premiers pas" or Settings;
// - one tip the first time each other tab is opened;
// - the "Premiers pas" checklist in the sidebar, ticked by real actions.
// The six-tab visit (startFeatureTour) stays available from Settings.
// The visit and the tutorial share the spotlight engine below.

const FEATURE_TOUR_STEPS = [
  { tab: 'chat', target: 'msg-input', fallback: 'tab-chat' },
  { tab: 'library', target: 'lib-search', fallback: 'tab-library' },
  { tab: 'discovery', target: 'discovery-search-form', fallback: 'tab-discovery' },
  { tab: 'drafting', target: 'drafting-actions', fallback: 'tab-drafting' },
  { tab: 'connections', target: 'cartography-header', fallback: 'tab-connections' },
  { tab: 'notebooks', target: 'nb-new-btn', fallback: 'tab-notebooks' },
];

const _ft = {
  active: false,
  tour: null,
  index: -1,
  currentTargetEl: null,
  resizeObserver: null,
  rafPending: false,
  stepCleanup: null,
};
let _ftEls = null;

function _ftEnsureDom() {
  if (_ftEls) return _ftEls;
  const root = document.createElement('div');
  root.id = 'feature-tour-root';
  root.hidden = true;
  root.innerHTML = `
    <div class="ft-backdrop"></div>
    <div class="ft-ring" aria-hidden="true"></div>
    <div class="ft-card" role="dialog" aria-modal="true" aria-labelledby="ft-title" aria-describedby="ft-body" tabindex="-1">
      <div class="ft-kicker" aria-live="polite"></div>
      <h2 id="ft-title"></h2>
      <p id="ft-body"></p>
      <div class="ft-dots" aria-hidden="true"></div>
      <div class="ft-actions">
        <button type="button" class="btn ft-skip"></button>
        <button type="button" class="btn ft-prev"></button>
        <button type="button" class="btn btn-cyan ft-next"></button>
      </div>
    </div>`;
  document.body.appendChild(root);
  _ftEls = {
    root,
    backdrop: root.querySelector('.ft-backdrop'),
    ring: root.querySelector('.ft-ring'),
    card: root.querySelector('.ft-card'),
    kicker: root.querySelector('.ft-kicker'),
    title: root.querySelector('#ft-title'),
    body: root.querySelector('#ft-body'),
    dots: root.querySelector('.ft-dots'),
    skip: root.querySelector('.ft-skip'),
    prev: root.querySelector('.ft-prev'),
    next: root.querySelector('.ft-next'),
  };
  _ftEls.skip.addEventListener('click', () => _ftEndTour({ completed: false }));
  _ftEls.prev.addEventListener('click', () => _ftPrev());
  _ftEls.next.addEventListener('click', () => _ftNext());
  return _ftEls;
}

function _ftIsVisible(el) {
  if (!el || !el.isConnected) return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

// A step target is an element id, or a function for elements that only
// exist once the user has acted (the answer bubble, its first citation).
function _ftStepElement(ref) {
  if (!ref) return null;
  return typeof ref === 'function' ? ref() : document.getElementById(ref);
}

function _ftResolveTarget(step) {
  const primary = _ftStepElement(step.target);
  if (_ftIsVisible(primary)) return primary;
  const fallback = _ftStepElement(step.fallback);
  return fallback || primary || document.body;
}

function _ftBlockingDialogOpen() {
  // Modals that toggle an "open" class (settings, knowledge graph, startup
  // help) also run a closing transition: right after the class is removed
  // (e.g. by closeSettings(), just before this runs from the settings
  // button), getComputedStyle still reports the pre-transition, visible
  // values for a moment. The class itself is the only signal that is
  // correct synchronously, so it takes priority over a generic visibility
  // check for those known dialogs.
  const classGated = ['startup-help-modal', 'settings-panel', 'kg-modal'];
  if (classGated.some((id) => document.getElementById(id)?.classList.contains('open'))) return true;
  return Array.from(document.querySelectorAll('[role="dialog"]')).some((el) => (
    !classGated.includes(el.id)
    && !_ftEls?.card?.contains(el)
    && !el.closest('.onb-popover')
    && !el.hidden
  ));
}

function _ftRenderDots(activeIndex) {
  _ftEls.dots.innerHTML = _ft.tour.steps.map((_step, i) => (
    `<span class="ft-dot${i === activeIndex ? ' is-active' : ''}"></span>`
  )).join('');
}

function _ftReplayCardEnter(card = _ftEls.card) {
  card.classList.remove('ft-card-enter');
  void card.offsetWidth;
  card.classList.add('ft-card-enter');
}

function _ftPositionSpotlight(rect) {
  const margin = 8;
  const top = Math.max(0, rect.top - margin);
  const left = Math.max(0, rect.left - margin);
  const width = rect.width + margin * 2;
  const height = rect.height + margin * 2;
  [_ftEls.backdrop, _ftEls.ring].forEach((el) => {
    el.style.top = `${top}px`;
    el.style.left = `${left}px`;
    el.style.width = `${width}px`;
    el.style.height = `${height}px`;
  });
}

// Right of the target, else left, else below, else above; always on screen.
function _ftPlaceCard(card, targetRect) {
  const margin = 12;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const cardRect = card.getBoundingClientRect();
  const w = cardRect.width || 360;
  const h = cardRect.height || 200;
  const clamp = (value, min, max) => Math.min(Math.max(value, min), Math.max(min, max));
  let top;
  let left;
  if (targetRect.right + margin + w <= vw) {
    left = targetRect.right + margin;
    top = clamp(targetRect.top, margin, vh - h - margin);
  } else if (targetRect.left - margin - w >= 0) {
    left = targetRect.left - margin - w;
    top = clamp(targetRect.top, margin, vh - h - margin);
  } else if (targetRect.bottom + margin + h <= vh) {
    top = targetRect.bottom + margin;
    left = clamp(targetRect.left, margin, vw - w - margin);
  } else {
    top = Math.max(margin, targetRect.top - margin - h);
    left = clamp(targetRect.left, margin, vw - w - margin);
  }
  card.style.top = `${top}px`;
  card.style.left = `${left}px`;
}

function _ftTeardownObserver() {
  if (_ft.resizeObserver) {
    _ft.resizeObserver.disconnect();
    _ft.resizeObserver = null;
  }
}

function _ftObserveTarget(el) {
  _ftTeardownObserver();
  if (!el || typeof ResizeObserver === 'undefined') return;
  _ft.resizeObserver = new ResizeObserver(() => _ftScheduleReposition());
  _ft.resizeObserver.observe(el);
}

function _ftRepositionCurrent() {
  if (!_ft.active || !_ft.tour || _ft.index < 0 || _ft.index >= _ft.tour.steps.length) return;
  const step = _ft.tour.steps[_ft.index];
  let target = _ft.currentTargetEl;
  if (!_ftIsVisible(target)) {
    target = _ftResolveTarget(step);
    if (target !== _ft.currentTargetEl) {
      _ft.currentTargetEl = target;
      _ftObserveTarget(target);
    }
  }
  const rect = target.getBoundingClientRect();
  _ftPositionSpotlight(rect);
  _ftPlaceCard(_ftEls.card, rect);
}

function _ftScheduleReposition() {
  if (_ft.rafPending || !_ft.active) return;
  _ft.rafPending = true;
  window.requestAnimationFrame(() => {
    _ft.rafPending = false;
    _ftRepositionCurrent();
  });
}

function _ftDoubleRaf() {
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => window.requestAnimationFrame(resolve));
  });
}

function _ftFocusNext() {
  window.setTimeout(() => {
    (_ftEls.next.hidden ? _ftEls.card : _ftEls.next).focus();
  }, 0);
}

function _ftClearStepWaiter() {
  if (typeof _ft.stepCleanup === 'function') _ft.stepCleanup();
  _ft.stepCleanup = null;
}

function _ftShowFinalCard(outcome) {
  const tour = _ft.tour;
  const done = (outcome && tour.outcomes?.[outcome]) || tour.done;
  _ftTeardownObserver();
  _ft.currentTargetEl = null;
  _ftEls.kicker.textContent = '';
  _ftEls.dots.hidden = true;
  _ftEls.title.textContent = _ti(done.titleKey);
  _ftEls.body.textContent = _ti(done.bodyKey);
  _ftEls.skip.hidden = true;
  _ftEls.prev.hidden = true;
  _ftEls.next.hidden = false;
  _ftEls.next.textContent = _ti(done.finishKey || 'tour.finish');
  _ftReplayCardEnter();
  _ftFocusNext();
}

async function _ftGoToStep(index, { outcome = '', toEnd = false } = {}) {
  _ftClearStepWaiter();
  const tour = _ft.tour;
  const steps = tour.steps;
  let next = toEnd ? steps.length : index;
  while (next < steps.length && steps[next].skipIf?.()) next += 1;
  _ft.index = next;
  const isFinal = next >= steps.length;
  const step = isFinal ? null : steps[next];
  _ftEls.root.classList.toggle('ft-final', isFinal);
  _ftEls.root.classList.toggle('ft-interactive', Boolean(step?.interactive));

  if (isFinal) {
    _ftShowFinalCard(outcome);
    return;
  }

  if (step.tab) switchTab(step.tab);
  step.onEnter?.();
  await _ftDoubleRaf();
  if (!_ft.active || _ft.tour !== tour || _ft.index !== next) return;

  const target = _ftResolveTarget(step);
  if (step.scrollIntoView && target !== document.body) {
    target.scrollIntoView({ block: 'center' });
  }
  _ft.currentTargetEl = target;
  _ftObserveTarget(target);

  _ftEls.kicker.textContent = _ti(tour.kickerKey, { n: next + 1, total: steps.length });
  _ftEls.title.textContent = _ti(step.titleKey);
  _ftEls.body.textContent = _ti(step.bodyKey);
  _ftEls.dots.hidden = false;
  _ftRenderDots(next);
  _ftEls.skip.hidden = false;
  _ftEls.skip.textContent = _ti(tour.skipKey);
  _ftEls.prev.hidden = !tour.allowPrev || next === 0;
  _ftEls.next.hidden = !step.nextKey;
  _ftEls.next.textContent = _ti(step.nextKey || 'tour.next');
  _ftReplayCardEnter();

  const rect = target.getBoundingClientRect();
  _ftPositionSpotlight(rect);
  _ftPlaceCard(_ftEls.card, rect);

  if (step.waitFor) {
    _ft.stepCleanup = step.waitFor((options = {}) => {
      if (_ft.active && _ft.tour === tour && _ft.index === next) void _ftGoToStep(next + 1, options);
    });
  }
  if (step.focus) window.setTimeout(step.focus, 0);
  else _ftFocusNext();
}

function _ftNext() {
  if (!_ft.active) return;
  if (_ft.index >= _ft.tour.steps.length) {
    _ftEndTour({ completed: true });
    return;
  }
  void _ftGoToStep(_ft.index + 1);
}

function _ftPrev() {
  if (!_ft.active || _ft.index <= 0 || _ftEls.prev.hidden) return;
  void _ftGoToStep(_ft.index - 1);
}

async function _ftPersistFeatureTourDone() {
  try {
    localStorage.setItem(LS_PREFIX + 'feature_tour_done_v1', '1');
  } catch (_) {
    // Best effort only.
  }
  try {
    const r = await fetch('/api/app-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ feature_tour_done_v1: true }),
    });
    const d = await r.json();
    if (d?.ok) state.appSettings = d;
  } catch (_) {
    // A network error must not block closing the tour.
  }
}

function _ftEndTour({ completed = false } = {}) {
  if (!_ft.active) return;
  const tour = _ft.tour;
  _ft.active = false;
  _ftClearStepWaiter();
  _ftTeardownObserver();
  window.removeEventListener('resize', _ftScheduleReposition);
  window.removeEventListener('scroll', _ftScheduleReposition, true);
  document.removeEventListener('keydown', _ftHandleKeydown);
  _ftEls.root.hidden = true;
  _ftEls.root.classList.remove('ft-final', 'ft-interactive');
  _ft.tour = null;
  tour?.onEnd?.({ completed });
}

function _ftHandleKeydown(event) {
  if (!_ft.active) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    _ftEndTour({ completed: false });
    return;
  }
  // During a hands-on step the user types in the app itself (Enter sends
  // the question): only keys pressed inside the card belong to the tour.
  const step = _ft.tour?.steps[_ft.index];
  if (step?.interactive && !_ftEls.card.contains(document.activeElement)) return;
  if (event.key === 'ArrowRight') {
    event.preventDefault();
    if (!_ftEls.next.hidden) _ftNext();
    return;
  }
  if (event.key === 'ArrowLeft') {
    event.preventDefault();
    _ftPrev();
    return;
  }
  if (event.key === 'Enter') {
    const active = document.activeElement;
    if (active === _ftEls.skip || active === _ftEls.prev || active === _ftEls.next) return;
    event.preventDefault();
    if (!_ftEls.next.hidden) _ftNext();
    return;
  }
  if (event.key !== 'Tab' || step?.interactive) return;
  const focusable = [_ftEls.skip, _ftEls.prev, _ftEls.next].filter((el) => !el.hidden);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (!_ftEls.card.contains(document.activeElement) || document.activeElement === _ftEls.card) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  } else if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function _ftBeginTour(tour) {
  _ftEnsureDom();
  closeOnboardingPopovers();
  _ftEls.prev.textContent = _ti('tour.prev');
  _ft.tour = tour;
  _ft.active = true;
  _ftEls.root.hidden = false;
  window.addEventListener('resize', _ftScheduleReposition, { passive: true });
  window.addEventListener('scroll', _ftScheduleReposition, { passive: true, capture: true });
  document.addEventListener('keydown', _ftHandleKeydown);
  void _ftGoToStep(0);
}

// ---------------------------------------------
//  SIX-TAB VISIT (Settings)
// ---------------------------------------------
function startFeatureTour() {
  if (_ft.active || _ftBlockingDialogOpen()) return;
  _ftBeginTour({
    kickerKey: 'tour.kicker',
    skipKey: 'tour.skip',
    allowPrev: true,
    steps: FEATURE_TOUR_STEPS.map((step) => ({
      ...step,
      titleKey: `tour.step.${step.tab}.title`,
      bodyKey: `tour.step.${step.tab}.body`,
      nextKey: 'tour.next',
    })),
    done: { titleKey: 'tour.done.title', bodyKey: 'tour.done.body', finishKey: 'tour.finish' },
    onEnd: () => {
      void _ftPersistFeatureTourDone();
      switchTab('chat');
      window.setTimeout(() => document.getElementById('msg-input')?.focus(), 120);
    },
  });
}

function _closeSettingsIfOpen() {
  if (document.getElementById('settings-panel')?.classList.contains('open')) {
    toggleSettings();
  }
}

function reopenFeatureTour() {
  _closeSettingsIfOpen();
  startFeatureTour();
}

// ---------------------------------------------
//  TUTORIAL: first question, first citation
// ---------------------------------------------
function _ftOnMilestone(names, handler) {
  const wanted = new Set(names);
  const listener = (event) => {
    if (wanted.has(event.detail?.name)) handler(event.detail);
  };
  window.addEventListener('bibliobrain:milestone', listener);
  return () => window.removeEventListener('bibliobrain:milestone', listener);
}

function _tutorialLatestAnswer() {
  const wraps = document.querySelectorAll('#messages .msg-wrap.bot');
  return wraps.length ? wraps[wraps.length - 1].querySelector('.bubble') : null;
}

function _tutorialCitationTarget() {
  const answer = _tutorialLatestAnswer();
  return answer?.querySelector('.assistant-citation-link')
    || answer?.querySelector('.assistant-sources-summary')
    || null;
}

const TUTORIAL_STEPS = [
  {
    id: 'ask',
    tab: 'chat',
    target: 'chat-composer-box',
    titleKey: 'tutorial.ask.title',
    bodyKey: 'tutorial.ask.body',
    interactive: true,
    onEnter: () => {
      if (state.activeChatId) startNewChat();
      const input = document.getElementById('msg-input');
      if (input && !input.value.trim()) {
        input.value = _ti('tutorial.question');
        input.dataset.tutorialPrefill = input.value;
        input.dispatchEvent(new Event('input', { bubbles: true }));
      }
    },
    focus: () => {
      const input = document.getElementById('msg-input');
      if (!input) return;
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    },
    waitFor: (advance) => _ftOnMilestone(['question-asked'], () => advance()),
  },
  {
    id: 'wait',
    tab: 'chat',
    target: _tutorialLatestAnswer,
    fallback: 'messages',
    titleKey: 'tutorial.wait.title',
    bodyKey: 'tutorial.wait.body',
    interactive: true,
    waitFor: (advance) => _ftOnMilestone(['answer-ready'], (detail) => {
      advance(detail.ok ? {} : { toEnd: true, outcome: 'failed' });
    }),
  },
  {
    id: 'cite',
    tab: 'chat',
    target: _tutorialCitationTarget,
    titleKey: 'tutorial.cite.title',
    bodyKey: 'tutorial.cite.body',
    nextKey: 'tutorial.skipStep',
    interactive: true,
    scrollIntoView: true,
    // An answer without any source has nothing to click: go straight on.
    skipIf: () => !_tutorialCitationTarget(),
    waitFor: (advance) => {
      let timer = 0;
      const done = () => {
        if (timer) return;
        // Leave a moment to see the source card open before moving on.
        timer = window.setTimeout(() => advance(), 1400);
      };
      const stopMilestone = _ftOnMilestone(['source-opened'], done);
      const onClick = (event) => {
        if (!event.target.closest?.('.assistant-sources-summary')) return;
        recordOnboardingMilestone('source-opened');
        done();
      };
      document.addEventListener('click', onClick, true);
      return () => {
        stopMilestone();
        document.removeEventListener('click', onClick, true);
        window.clearTimeout(timer);
      };
    },
  },
  {
    id: 'depth',
    tab: 'chat',
    target: 'chat-depth',
    titleKey: 'tutorial.depth.title',
    bodyKey: 'tutorial.depth.body',
    nextKey: 'tour.next',
    interactive: true,
  },
];

function _tutorialClearUnsentPrefill() {
  const input = document.getElementById('msg-input');
  if (!input?.dataset.tutorialPrefill) return;
  if (input.value === input.dataset.tutorialPrefill) {
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  delete input.dataset.tutorialPrefill;
}

function startChatTutorial() {
  if (_ft.active) return;
  _closeSettingsIfOpen();
  if (!state.ragReady) {
    showToast(_ti('tutorial.needsSetup'), 3600, 'info');
    return;
  }
  if (state.streaming) {
    showToast(_ti('tutorial.busy'), 3200, 'info');
    return;
  }
  if (_ftBlockingDialogOpen()) return;
  _ftBeginTour({
    kickerKey: 'tutorial.kicker',
    skipKey: 'tutorial.quit',
    allowPrev: false,
    steps: TUTORIAL_STEPS,
    done: { titleKey: 'tutorial.done.title', bodyKey: 'tutorial.done.body', finishKey: 'tutorial.finish' },
    outcomes: {
      failed: { titleKey: 'tutorial.failed.title', bodyKey: 'tutorial.failed.body', finishKey: 'tutorial.finish' },
    },
    onEnd: ({ completed }) => {
      _tutorialClearUnsentPrefill();
      if (completed) void saveOnboardingProgress({ tutorial_done: true });
      window.setTimeout(() => document.getElementById('msg-input')?.focus(), 120);
    },
  });
}

// ---------------------------------------------
//  ONBOARDING PROGRESS (tips + "Premiers pas")
// ---------------------------------------------
const ONBOARDING_TIPS = {
  library: 'lib-search',
  discovery: 'discovery-search-form',
  drafting: 'drafting-btn',
  connections: 'cartography-header',
  notebooks: 'nb-new-btn',
};

const ONBOARDING_CHECKLIST = [
  { id: 'prepared', action: () => openStartupAssistant() },
  { id: 'question-asked', action: () => startChatTutorial() },
  { id: 'source-opened', action: () => startChatTutorial() },
  { id: 'citation-inserted', action: () => showOnboardingTip('drafting') },
  { id: 'notebook-created', action: () => showOnboardingTip('notebooks') },
];

const _onb = {
  progress: null,
  loading: null,
  tip: null,
  panel: null,
};

function _onbCurrentProgress() {
  return _onb.progress || state.appSettings?.onboarding_progress || null;
}

async function _onbLoadProgress() {
  const known = _onbCurrentProgress();
  if (known) {
    _onb.progress = known;
    return known;
  }
  if (!_onb.loading) {
    _onb.loading = fetch('/api/app-settings', { cache: 'no-store' })
      .then((response) => response.json())
      .then((settings) => {
        if (settings?.onboarding_progress) _onb.progress = settings.onboarding_progress;
        return _onb.progress;
      })
      .catch(() => null)
      .finally(() => {
        _onb.loading = null;
      });
  }
  return _onb.loading;
}

async function saveOnboardingProgress(patch) {
  const current = (await _onbLoadProgress()) || {
    tutorial_done: false,
    tips_seen: [],
    milestones: [],
    checklist_hidden: false,
  };
  _onb.progress = { ...current, ...patch };
  refreshOnboardingChecklist();
  try {
    const response = await fetch('/api/app-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ onboarding_progress: _onb.progress }),
    });
    const settings = await response.json();
    if (settings?.ok) state.appSettings = settings;
  } catch (_) {
    // The local copy still drives this session.
  }
}

function recordOnboardingMilestone(name) {
  if (!ONBOARDING_CHECKLIST.some((item) => item.id === name && name !== 'prepared')) return;
  void _onbLoadProgress().then((progress) => {
    const milestones = progress?.milestones || [];
    if (milestones.includes(name)) return;
    void saveOnboardingProgress({ milestones: [...milestones, name] });
  });
}

function _onbSetupDone() {
  const settings = state.startupStatus?.settings || {};
  return Boolean(
    state.ragReady
    || state.startupStatus?.rag?.ready
    || settings.first_rag_init_done
    || settings.onboarding_done_v1,
  );
}

function _onbItemDone(id, progress) {
  if (id === 'prepared') {
    return Boolean(state.ragReady || state.startupStatus?.settings?.first_rag_init_done);
  }
  return Boolean(progress?.milestones?.includes(id));
}

function _onbPlacePopover(popover, anchorRect) {
  _ftPlaceCard(popover, anchorRect);
}

function _onbTrackPopover(popover, anchor, onDismiss) {
  const reposition = () => {
    if (anchor.isConnected) _onbPlacePopover(popover, anchor.getBoundingClientRect());
  };
  const onKeydown = (event) => {
    if (event.key === 'Escape') onDismiss();
  };
  const onPointerDown = (event) => {
    if (!popover.contains(event.target) && !anchor.contains(event.target)) onDismiss();
  };
  window.addEventListener('resize', reposition, { passive: true });
  document.addEventListener('keydown', onKeydown);
  // Registered after the click that opened the popover has finished.
  const pointerTimer = window.setTimeout(() => {
    document.addEventListener('pointerdown', onPointerDown, true);
  }, 0);
  return () => {
    window.clearTimeout(pointerTimer);
    window.removeEventListener('resize', reposition);
    document.removeEventListener('keydown', onKeydown);
    document.removeEventListener('pointerdown', onPointerDown, true);
  };
}

// ---- Tips: one per tab, the first time it is opened ----
function closeOnboardingTip() {
  const tip = _onb.tip;
  if (!tip) return;
  _onb.tip = null;
  tip.cleanup();
  tip.anchor.classList.remove('onb-tip-target');
  tip.el.remove();
}

function closeOnboardingPopovers() {
  closeOnboardingTip();
  closeOnboardingChecklist();
}

async function _onbShowTip(tab, { force = false } = {}) {
  const targetId = ONBOARDING_TIPS[tab];
  if (!targetId || _ft.active) return;
  const progress = await _onbLoadProgress();
  if (!force && (!progress || progress.tips_seen.includes(tab) || !_onbSetupDone())) return;
  // Let the tab paint (and lazy panels load) before measuring the target.
  await new Promise((resolve) => window.setTimeout(resolve, 350));
  const panel = document.getElementById(`tab-${tab}`);
  const anchor = document.getElementById(targetId);
  if (!panel?.classList.contains('active') || !_ftIsVisible(anchor)) return;
  if (_ft.active || _ftBlockingDialogOpen()) return;

  closeOnboardingPopovers();
  const el = document.createElement('div');
  el.className = 'ft-card onb-popover onb-tip';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'false');
  el.setAttribute('aria-labelledby', 'onb-tip-title');
  el.innerHTML = `
    <div class="ft-kicker"></div>
    <h2 id="onb-tip-title"></h2>
    <p></p>
    <div class="ft-actions"><button type="button" class="btn btn-cyan onb-tip-ok"></button></div>`;
  el.querySelector('.ft-kicker').textContent = _ti('tip.kicker');
  el.querySelector('h2').textContent = _ti(`tip.${tab}.title`);
  el.querySelector('p').textContent = _ti(`tip.${tab}.body`);
  const ok = el.querySelector('.onb-tip-ok');
  ok.textContent = _ti('tip.gotIt');
  ok.addEventListener('click', closeOnboardingTip);
  document.body.appendChild(el);
  anchor.classList.add('onb-tip-target');
  _onbPlacePopover(el, anchor.getBoundingClientRect());
  _ftReplayCardEnter(el);
  _onb.tip = { el, anchor, cleanup: _onbTrackPopover(el, anchor, closeOnboardingTip) };
  window.setTimeout(() => ok.focus({ preventScroll: true }), 0);

  if (progress && !progress.tips_seen.includes(tab)) {
    void saveOnboardingProgress({ tips_seen: [...progress.tips_seen, tab] });
  }
}

function showOnboardingTip(tab) {
  closeOnboardingChecklist();
  switchTab(tab);
  void _onbShowTip(tab, { force: true });
}

window.addEventListener('bibliobrain:tabchange', (event) => {
  closeOnboardingTip();
  const tab = event.detail?.name;
  if (tab) void _onbShowTip(tab);
});

// ---- "Premiers pas" checklist ----
function closeOnboardingChecklist() {
  const panel = _onb.panel;
  if (!panel) return;
  _onb.panel = null;
  panel.cleanup();
  panel.el.remove();
  document.getElementById('onboarding-checklist-btn')?.setAttribute('aria-expanded', 'false');
}

function _onbRenderChecklistPanel(el, progress) {
  const doneCount = ONBOARDING_CHECKLIST.filter((item) => _onbItemDone(item.id, progress)).length;
  const allDone = doneCount === ONBOARDING_CHECKLIST.length;
  el.innerHTML = `
    <div class="onb-cl-head">
      <h2 id="onb-cl-title"></h2>
      <span class="onb-cl-head-count"></span>
      <button type="button" class="onb-cl-close"><span aria-hidden="true">&times;</span></button>
    </div>
    <p class="onb-cl-done" hidden></p>
    <ul class="onb-cl-list"></ul>
    <div class="onb-cl-foot"><button type="button" class="startup-link-btn onb-cl-hide"></button></div>`;
  el.querySelector('h2').textContent = _ti('onboarding.checklistTitle');
  el.querySelector('.onb-cl-head-count').textContent = `${doneCount}/${ONBOARDING_CHECKLIST.length}`;
  const close = el.querySelector('.onb-cl-close');
  close.setAttribute('aria-label', _ti('onboarding.close'));
  close.addEventListener('click', closeOnboardingChecklist);
  const doneNote = el.querySelector('.onb-cl-done');
  doneNote.hidden = !allDone;
  doneNote.textContent = _ti('onboarding.allDone');

  const list = el.querySelector('.onb-cl-list');
  ONBOARDING_CHECKLIST.forEach((item) => {
    const done = _onbItemDone(item.id, progress);
    const row = document.createElement('li');
    row.className = done ? 'is-done' : '';
    const mark = document.createElement('span');
    mark.className = 'onb-cl-mark';
    mark.setAttribute('aria-hidden', 'true');
    mark.textContent = done ? '✓' : '';
    const label = document.createElement('span');
    label.className = 'onb-cl-label';
    label.textContent = _ti(`onboarding.item.${item.id}`);
    row.append(mark, label);
    if (done) {
      const status = document.createElement('span');
      status.className = 'visually-hidden';
      status.textContent = _ti('onboarding.doneLabel');
      row.appendChild(status);
    } else {
      const show = document.createElement('button');
      show.type = 'button';
      show.className = 'btn btn-ghost onb-cl-show';
      show.textContent = _ti('onboarding.show');
      show.addEventListener('click', () => {
        closeOnboardingChecklist();
        item.action();
      });
      row.appendChild(show);
    }
    list.appendChild(row);
  });

  const hide = el.querySelector('.onb-cl-hide');
  hide.textContent = _ti('onboarding.hide');
  hide.addEventListener('click', () => {
    closeOnboardingChecklist();
    void saveOnboardingProgress({ checklist_hidden: true });
    showToast(_ti('onboarding.hiddenToast'), 3600, 'info');
  });
}

function toggleOnboardingChecklist() {
  if (_onb.panel) {
    closeOnboardingChecklist();
    return;
  }
  const button = document.getElementById('onboarding-checklist-btn');
  const progress = _onbCurrentProgress();
  if (!button || !progress) return;
  closeOnboardingTip();
  const el = document.createElement('div');
  el.className = 'ft-card onb-popover onb-checklist';
  el.id = 'onboarding-checklist';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-modal', 'false');
  el.setAttribute('aria-labelledby', 'onb-cl-title');
  _onbRenderChecklistPanel(el, progress);
  document.body.appendChild(el);
  _onbPlacePopover(el, button.getBoundingClientRect());
  _ftReplayCardEnter(el);
  _onb.panel = { el, cleanup: _onbTrackPopover(el, button, closeOnboardingChecklist) };
  button.setAttribute('aria-expanded', 'true');
  window.setTimeout(() => el.querySelector('.onb-cl-show, .onb-cl-close')?.focus(), 0);
}

function refreshOnboardingChecklist() {
  const button = document.getElementById('onboarding-checklist-btn');
  if (!button) return;
  const progress = _onbCurrentProgress();
  const visible = Boolean(progress && !progress.checklist_hidden && _onbSetupDone());
  button.hidden = !visible;
  if (!visible) {
    closeOnboardingChecklist();
    return;
  }
  const total = ONBOARDING_CHECKLIST.length;
  const done = ONBOARDING_CHECKLIST.filter((item) => _onbItemDone(item.id, progress)).length;
  const count = document.getElementById('onboarding-checklist-count');
  const bar = document.getElementById('onboarding-checklist-bar');
  if (count) count.textContent = `${done}/${total}`;
  if (bar) bar.style.width = `${Math.round((done / total) * 100)}%`;
  button.title = _ti('onboarding.progressAria', { done, total });
  button.setAttribute('aria-label', `${_ti('onboarding.checklistTitle')} · ${button.title}`);
  if (_onb.panel) _onbRenderChecklistPanel(_onb.panel.el, progress);
}

async function resetOnboardingHelp() {
  await saveOnboardingProgress({ tips_seen: [], checklist_hidden: false });
  showToast(_ti('onboarding.resetDone'), 3200, 'success');
}

window.addEventListener('bibliobrain:milestone', (event) => {
  recordOnboardingMilestone(event.detail?.name);
});
window.addEventListener('bibliobrain:languagechange', () => {
  closeOnboardingTip();
  refreshOnboardingChecklist();
});
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('onboarding-checklist-btn')
    ?.addEventListener('click', toggleOnboardingChecklist);
  document.getElementById('tutorial-start-btn')
    ?.addEventListener('click', startChatTutorial);
  document.getElementById('onboarding-reset-btn')
    ?.addEventListener('click', () => void resetOnboardingHelp());
  void _onbLoadProgress().then(refreshOnboardingChecklist);
}, { once: true });
