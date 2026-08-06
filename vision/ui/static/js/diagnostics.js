// diagnostics.js — полностью переписан под новый блок анализа кадра (как пороги правил)
'use strict';

function disableSelectedAnalysisButton() {
    if (els.analyzeSelectedFrame) els.analyzeSelectedFrame.disabled = true;
}

function updateDistributorDiagnosticControls(ls) {
    if (!els.distributorDiagnostics) return;
    const allowed = (
        ls.diagnostic_allowed === true
        && (!ls.controls || ls.controls.distributor_diagnostic === true)
        && !state.offline
    );
    state.distributorDiagnosticBackendBusy = ls.diagnostic_busy === true;
    const busy = (
        state.distributorDiagnosticBackendBusy
        || state.distributorDiagnosticPending
    );
    els.distributorDiagnostics.querySelectorAll('button').forEach(button => {
        button.disabled = !allowed || busy;
        button.classList.toggle('pending', busy);
    });
}

function setupDistributorDiagnostics() {
    if (!els.distributorDiagnostics) return;
    els.distributorDiagnostics.querySelectorAll('button').forEach(button => {
        button.addEventListener('click', async () => {
            if (button.disabled || state.distributorDiagnosticPending) return;
            const command = button.dataset.distributorCommand;
            if (!command) return;
            state.distributorDiagnosticPending = true;
            startStatusPolling();
            updateViewModeControls();
            state.backendControls = {
                ...state.backendControls,
                start: false,
                exit: false,
                jog_hold: false,
                distributor_diagnostic: false,
                camera_diagnostic: false,
                vision_rule_diagnostic: false,
            };
            applyButtonsForState(
                state.lineState,
                state.serverExitRequested,
                state.backendControls,
            );
            if (els.jogPanel) {
                els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(
                    jogButton => { jogButton.disabled = true; }
                );
            }
            disableSelectedAnalysisButton();
            updateDistributorDiagnosticControls({
                diagnostic_allowed: false,
                diagnostic_busy: true,
            });
            try {
                clearControlError();
                await apiPost(
                    `/api/distributor/diagnostic/${command}`,
                    true,
                );
            } finally {
                state.distributorDiagnosticPending = false;
                startStatusPolling();
                updateViewModeControls();
                requestImmediateStatus();
            }
        });
    });
}

function updateSelectedAnalysisStatus(ls) {
    if (!els.analyzeSelectedFrame) return;
    const selected = ls.selected_analysis || {};
    const wasActive = state.selectedAnalysisActive;
    const wasLiveStreaming = state.liveStreaming;
    state.selectedAnalysisActive = selected.active === true;
    state.selectedAnalysisRole = selected.role || null;
    if (wasActive !== state.selectedAnalysisActive
        && typeof updateThresholdsPanel === 'function') {
        updateThresholdsPanel();
    }

    // Панель «Анализ кадра» схлопывает обычные блоки правой колонки
    // (showPendingSelectedFrameAnalysis) и обязана вернуть их после
    // завершения анализа. Раньше это делал updateFrameAnalysisStatus,
    // который больше нигде не вызывается: из-за этого статистика
    // корпусов, распределитель и «Пустые лотки» оставались свёрнутыми
    // навсегда после «ВЕРНУТЬ ПОТОК». Восстановление выполняется здесь,
    // на каждом тике статуса, идемпотентно.
    const selectedActive = (
        state.selectedAnalysisActive
        && JOG_ALLOWED_STATES.includes(state.lineState)
    );
    if (els.statsSummary) {
        els.statsSummary.classList.toggle('is-collapsed', selectedActive);
    }
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.classList.toggle('is-collapsed', selectedActive);
    }
    if (els.statsService) {
        els.statsService.classList.toggle('is-collapsed', selectedActive);
    }
    const live = ls.live || {};
    state.liveFps = Number(live.fps || (ls.jog || {}).live_fps || 0);
    state.liveStreaming = live.streaming === true;
    state.liveStatic = live.static === true;

    if (wasLiveStreaming !== state.liveStreaming) {
        if (typeof applyMainCameraSource === 'function') {
            applyMainCameraSource();
        }
    }

    const controls = ls.controls || {};
    const allowed = state.selectedAnalysisActive
        ? controls.selected_model_release === true
        : controls.selected_model_analysis === true;

    const lineState = state.lineState;
    const showAnalysis = (lineState === 'IDLE' || lineState === 'STOPPED') || state.selectedAnalysisActive;
    els.analyzeSelectedFrame.classList.toggle('is-hidden', !showAnalysis);

    els.analyzeSelectedFrame.disabled = (
        !allowed
        || state.selectedAnalysisPending
        || state.offline
        || !state.currentCamera
    );
    els.analyzeSelectedFrame.textContent = state.selectedAnalysisActive
        ? 'ВЕРНУТЬ ПОТОК'
        : 'АНАЛИЗ КАДРА';
    els.analyzeSelectedFrame.classList.toggle(
        'analysis-active',
        state.selectedAnalysisActive,
    );
    applyLiveBadge(state.jogActive);
    updateViewModeControls();

    if (state.selectedAnalysisActive) {
        showSelectedAnalysisFrame(state.selectedAnalysisRole);
    } else if (wasActive) {
        returnSelectedCameraToLive();
    }
}

// ─── Новый анализ кадра — как пороги правил ────────────────────────

function frameAnalysisVerdict(report, ls) {
    if (!report || report.kind !== 'CYCLE') return '';
    const rules = Array.isArray(report.rules) ? report.rules : [];
    const partId = report.part_id;

    if (partId == null) {
        return rules.some(rule => rule.part_absent === true)
            ? 'КОРПУС НЕ ОБНАРУЖЕН'
            : '';
    }

    const parts = Array.isArray(ls.line_parts) ? ls.line_parts : [];
    const part = parts.find(p => Number(p.id) === Number(partId));
    const category = part ? String(part.category || '').toUpperCase() : '';
    const triggered = rules.some(rule => rule.triggered === true);
    const stage = String(report.stage || '').toUpperCase();

    if (stage.includes('КОНТРОЛЬ')) {
        if (category === 'BAD' || category === 'CLEANUP') {
            return 'РЕШЕНИЕ: ' + categoryLabel(category);
        }
        return triggered ? 'РЕШЕНИЕ: ЕСТЬ ДЕФЕКТЫ' : 'РЕШЕНИЕ: ГОДНО';
    }

    if (category === 'BAD') return 'ВХОД: РЕШЕНИЕ — БРАК';
    if (category === 'CLEANUP') return 'ВХОД: РЕШЕНИЕ — НА ОЧИСТКУ';
    if (triggered) return 'ВХОД: ЕСТЬ СРАБОТАВШИЕ ПРАВИЛА';
    return 'ВХОД: КОРПУС ПРИНЯТ, ДЕФЕКТОВ НЕТ';
}

function frameAnalysisContext(report) {
    if (!report) return '';
    const parts = [];
    const stage = String(report.stage || '').toUpperCase();
    const roleLabel = report.role ? cameraRoleLabel(report.role) : '';
    if (stage && !roleLabel.toUpperCase().startsWith(stage)) {
        parts.push(stage);
    }
    if (roleLabel) {
        parts.push(roleLabel);
    }
    if (report.part_id != null) {
        parts.push('КОРПУС #' + report.part_id);
    }
    return parts.join(' · ');
}

function decisiveRules(rules) {
    const absent = rules.find(rule => rule.part_absent === true);
    if (absent) return [absent];
    return rules;
}

function triggeredRules(rules) {
    const base = decisiveRules(rules);
    const filtered = base.filter(rule => (
        rule.part_absent === true
        || rule.triggered === true
        || rule.skipped === true
    ));
    return filtered.length ? filtered : base;
}

function visibleFrameAnalysisRules(rules) {
    const base = decisiveRules(rules);
    if (state.frameAnalysisRulesFilter === 'all') return base;
    return triggeredRules(rules);
}

function setupFrameAnalysisModelsToggle() {
    const toggle = document.getElementById('frame-analysis-models-toggle');
    const list = document.getElementById('frame-analysis-models');
    if (!toggle || !list || toggle.dataset.bound === '1') return;
    toggle.dataset.bound = '1';
    toggle.addEventListener('click', () => {
        const col = list.classList.toggle('frame-analysis-list-collapsed');
        toggle.setAttribute('aria-expanded', col ? 'false' : 'true');
    });
}

function updateFrameAnalysisModels(models) {
    setupFrameAnalysisModelsToggle();
    const list = document.getElementById('frame-analysis-models');
    if (!list) return;
    list.innerHTML = '';
    (Array.isArray(models) ? models : []).forEach(m => {
        const item = document.createElement('div');
        item.className = 'frame-analysis-item';
        item.textContent = (m.role || '') + ': ' + (m.model || '');
        list.appendChild(item);
    });
}

function updateFrameAnalysisRulesTitle(rules) {
    const el = els.frameAnalysisRulesTitle || document.getElementById('frame-analysis-rules-title');
    if (!el) return;
    const all = decisiveRules(rules || []);
    const bad = triggeredRules(rules || []);
    const showing = state.frameAnalysisRulesFilter === 'all' ? all : bad;
    const label = state.frameAnalysisRulesFilter === 'all'
        ? 'ПРАВИЛА · ' + showing.length
        : (bad.length === all.length && !all.some(r => r.triggered || r.part_absent || r.skipped)
            ? 'ПРАВИЛА · ' + showing.length
            : 'ПРАВИЛА · ' + showing.length + '/' + all.length);
    setIfChanged(el, label);
}

function updateFrameAnalysisFilterButtons() {
    const filter = state.frameAnalysisRulesFilter === 'all' ? 'all' : 'triggered';
    const trig = els.frameAnalysisFilterTriggered || document.getElementById('frame-analysis-filter-triggered');
    const all = els.frameAnalysisFilterAll || document.getElementById('frame-analysis-filter-all');
    if (trig) {
        const on = filter === 'triggered';
        trig.classList.toggle('is-active', on);
        trig.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    if (all) {
        const on = filter === 'all';
        all.classList.toggle('is-active', on);
        all.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
}

function setFrameAnalysisRulesFilter(next) {
    const value = next === 'all' ? 'all' : 'triggered';
    if (state.frameAnalysisRulesFilter === value) return;
    state.frameAnalysisRulesFilter = value;
    updateFrameAnalysisFilterButtons();
    if (state.frameAnalysisRulesCache) {
        updateFrameAnalysisRulesTitle(state.frameAnalysisRulesCache);
        if (typeof renderFrameAnalysisPanel === 'function') {
            const vis = visibleFrameAnalysisRules(state.frameAnalysisRulesCache);
            renderFrameAnalysisPanel(vis, state.pictureRun, state.frameAnalysisModelsCache, {
                totalRules: state.frameAnalysisRulesCache.length,
            });
        } else if (typeof renderFrameAnalysisRules === 'function') {
            renderFrameAnalysisRules(state.frameAnalysisRulesCache, state.pictureRun);
        }
    }
}

function ruleSummaryLines(rule) {
    if (Array.isArray(rule.summary_lines) && rule.summary_lines.length) {
        return rule.summary_lines.filter(Boolean).map(String);
    }
    const detailLines = Array.isArray(rule.detail_lines)
        ? rule.detail_lines.filter(Boolean).map(String)
        : [];
    if (detailLines.length) return detailLines;
    return rule.detail ? [String(rule.detail)] : [];
}

function appendFaEmpty(container, text) {
    if (!container) return;
    const item = document.createElement('div');
    item.className = 'fa-empty';
    item.textContent = text;
    container.appendChild(item);
}

// Клик по замеру [Значение] → показать кадр того же набора на главной
// камере. Тройное голосование убрано: набор один, у замеров нет data-run,
// переключение недоступно — обработчик не нужен.

function setupFrameAnalysisFilter() {
    const trig = els.frameAnalysisFilterTriggered || document.getElementById('frame-analysis-filter-triggered');
    const all = els.frameAnalysisFilterAll || document.getElementById('frame-analysis-filter-all');
    if (trig) {
        trig.addEventListener('click', () => setFrameAnalysisRulesFilter('triggered'));
    }
    if (all) {
        all.addEventListener('click', () => setFrameAnalysisRulesFilter('all'));
    }
    updateFrameAnalysisFilterButtons();
}

function showPendingSelectedFrameAnalysis() {
    if (!els.frameAnalysisPanel) return;
    state.frameAnalysisRulesCache = null;
    state.pictureRun = 0;
    els.frameAnalysisPanel.classList.remove('is-collapsed');
    if (els.statsSummary) els.statsSummary.classList.add('is-collapsed');
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.classList.add('is-collapsed');
    }
    if (els.statsService) els.statsService.classList.add('is-collapsed');
    setIfChanged(els.frameAnalysisTitle, 'АНАЛИЗ КАДРА');
    setIfChanged(els.frameAnalysisVerdict, 'Подготовка моделей и правил');
    setIfChanged(els.frameAnalysisRulesTitle, 'ПРАВИЛА');
    const tabs = els.frameAnalysisTabs || document.getElementById('frame-analysis-tabs');
    const cards = els.frameAnalysisCards || document.getElementById('frame-analysis-cards');
    if (tabs) tabs.innerHTML = '';
    if (cards) {
        cards.innerHTML = '';
        appendFaEmpty(cards, 'Ожидание результатов');
    }
    // legacy fallback
    if (els.frameAnalysisRules) {
        els.frameAnalysisRules.replaceChildren();
    }
}

function updateFrameAnalysisStatus(ls) {
    if (!els.frameAnalysisPanel) return;
    const report = ls.frame_analysis || {};
    const available = report.available === true;
    const selectedActive = (
        state.selectedAnalysisActive
        && JOG_ALLOWED_STATES.includes(state.lineState)
    );
    const production = ['RUNNING', 'STOPPING'].includes(state.lineState);

    els.statsPanel.classList.toggle('production-view', production);
    els.statsPanel.classList.toggle('selected-frame-view', selectedActive);
    els.frameAnalysisPanel.classList.toggle('is-collapsed', !available);
    if (els.statsSummary) {
        els.statsSummary.classList.toggle('is-collapsed', selectedActive);
    }
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.classList.toggle('is-collapsed', selectedActive);
    }
    if (els.statsService) {
        els.statsService.classList.toggle('is-collapsed', selectedActive);
    }
    if (els.defectsSection && selectedActive) {
        els.defectsSection.classList.add('is-hidden');
    }

    if (!available) {
        state.lastFrameAnalysisRenderKey = null;
        state.frameAnalysisRulesCache = null;
        state.pictureRun = 0;
        return;
    }

    const rules = Array.isArray(report.rules) ? report.rules : [];
    const verdict = frameAnalysisVerdict(report, ls) || report.message || '—';
    setIfChanged(els.frameAnalysisTitle, report.title || 'АНАЛИЗ КАДРА');
    setIfChanged(els.frameAnalysisVerdict, verdict);
    const msgEl = els.frameAnalysisMessage || document.getElementById('frame-analysis-message');
    if (msgEl) setIfChanged(msgEl, verdict);
    const ctxEl = els.frameAnalysisContext || document.getElementById('frame-analysis-context');
    if (ctxEl) setIfChanged(ctxEl, frameAnalysisContext(report));

    const picEl = els.frameAnalysisPicture || document.getElementById('frame-analysis-picture');
    if (report.picture_reason && picEl) {
        picEl.classList.remove('is-hidden');
        // Тройное голосование убрано: набор кадров один, номер не показываем.
        setIfChanged(picEl, 'КАРТИНКА: ' + report.picture_reason);
    } else if (picEl) {
        picEl.classList.add('is-hidden');
        setIfChanged(picEl, '');
    }

    updateFrameAnalysisFilterButtons();
    updateFrameAnalysisRulesTitle(rules);
    updateFrameAnalysisModels(report.models);

    const renderKey = JSON.stringify({
        kind: report.kind,
        role: report.role,
        part: report.part_id,
        updated: report.updated_at,
        rules,
        picture_run: report.picture_run,
    });
    if (state.lastFrameAnalysisRenderKey === renderKey) {
        // данные те же — но фильтр мог смениться, перерисуем с фильтром
        const vis = visibleFrameAnalysisRules(rules);
        if (typeof renderFrameAnalysisPanel === 'function') {
            renderFrameAnalysisPanel(vis, state.pictureRun, report.models, {
                totalRules: rules.length,
                status: report.status,
                message: report.message,
            });
        }
        return;
    }
    state.lastFrameAnalysisRenderKey = renderKey;

    const pictureRun = Number(report.picture_run) || 0;
    state.frameAnalysisRulesCache = rules;
    state.pictureRun = pictureRun;
    if (state.frameAnalysisRulesFilter !== 'all'
        && state.frameAnalysisRulesFilter !== 'triggered') {
        state.frameAnalysisRulesFilter = 'triggered';
    }

    const visible = visibleFrameAnalysisRules(rules);
    if (typeof renderFrameAnalysisPanel === 'function') {
        renderFrameAnalysisPanel(visible, pictureRun, report.models, {
            totalRules: rules.length,
            status: report.status,
            message: report.message,
        });
    } else if (typeof renderFrameAnalysisRules === 'function') {
        renderFrameAnalysisRules(visible, pictureRun);
    }

    // Сохранить модели для использования при смене фильтра
    state.frameAnalysisModelsCache = report.models;

    // анимация для новой карточки
    const cardsEl = els.frameAnalysisCards || document.getElementById('frame-analysis-cards');
    if (cardsEl) animateUiElement(cardsEl, 'ui-content-change');
}

// Совместимость: старый renderFrameAnalysisRules теперь прокси к новому
function renderFrameAnalysisRulesCompat(rules, pictureRun) {
    if (typeof renderFrameAnalysisPanel === 'function') {
        renderFrameAnalysisPanel(rules, pictureRun);
    }
}

// Старый рендеринг карточки правила — для совместимости с тестами UI
function renderLegacyRuleCard(rule, pictureRun) {
    // Формируем класс состояния карточки по rule.neutral (как в старом UI)
    const stateClass = rule.neutral ? 'is-neutral' : (rule.triggered ? 'is-bad' : 'is-ok');
    // Старая логика видимости: rule.triggered || rule.skipped || rule.show_detail
    const visible = rule.triggered || rule.skipped || rule.show_detail;
    if (!visible) return null;
    const detailLines = Array.isArray(rule.detail_lines)
        ? rule.detail_lines.filter(Boolean).map(String)
        : [];
    const lines = detailLines.length ? detailLines : (rule.detail ? [String(rule.detail)] : []);
    const card = document.createElement('div');
    card.className = 'frame-analysis-item' + (stateClass ? ' ' + stateClass : '');
    card.dataset.rule = rule.name || '';
    const head = document.createElement('div');
    const title = document.createElement('b');
    title.textContent = rule.status_label || (rule.triggered ? 'СРАБОТАЛО' : 'НОРМА');
    head.appendChild(title);
    const reason = document.createElement('span');
    reason.className = 'frame-analysis-reason';
    reason.textContent = rule.human_cause || (lines[0] || '');
    head.appendChild(reason);
    card.appendChild(head);
    if (lines.length > 1) {
        const body = document.createElement('div');
        body.className = 'frame-analysis-detail';
        lines.slice(1).forEach(line => {
            const row = document.createElement('div');
            row.className = 'frame-analysis-item-row';
            row.textContent = line;
            body.appendChild(row);
        });
        card.appendChild(body);
    }
    return card;
}

function setupSelectedFrameAnalysis() {
    setupFrameAnalysisFilter();
    if (!els.analyzeSelectedFrame) return;
    els.analyzeSelectedFrame.addEventListener('click', async () => {
        if (els.analyzeSelectedFrame.disabled || state.selectedAnalysisPending) return;
        state.selectedAnalysisPending = true;
        if (!state.selectedAnalysisActive) {
            showPendingSelectedFrameAnalysis();
        }
        updateViewModeControls();
        els.analyzeSelectedFrame.disabled = true;
        state.backendControls = {
            ...state.backendControls,
            start: false,
            jog_hold: false,
            distributor_diagnostic: false,
            camera_diagnostic: false,
            vision_rule_diagnostic: false,
            selected_model_analysis: false,
        };
        applyButtonsForState(
            state.lineState,
            state.serverExitRequested,
            state.backendControls,
        );
        if (els.distributorDiagnostics) {
            els.distributorDiagnostics.querySelectorAll('button').forEach(
                button => { button.disabled = true; }
            );
        }
        if (els.jogPanel) {
            els.jogPanel.classList.add('is-collapsed');
            els.jogPanel.querySelectorAll('.jog-hold-btn').forEach(
                button => { button.disabled = true; }
            );
        }
        clearControlError();
        try {
            if (state.selectedAnalysisActive) {
                await apiPost('/api/diagnostics/selected/release', true);
            } else {
                const role = state.currentCamera;
                if (!role) return;
                await apiPost(
                    '/api/diagnostics/selected/' + encodeURIComponent(role),
                    true,
                );
            }
        } finally {
            state.selectedAnalysisPending = false;
            updateViewModeControls();
            requestImmediateStatus();
        }
    });
}

// Экспорт для тестов/совместимости
// Рендер порогов правила (реализуется в frame-analysis.js / diagnostics.js)
// Вызывается из renderFrameAnalysisPanel и updateFrameAnalysisStatus
if (typeof window !== 'undefined') {
    window.visibleFrameAnalysisRules = visibleFrameAnalysisRules;
    window.updateFrameAnalysisFilterButtons = updateFrameAnalysisFilterButtons;
}
