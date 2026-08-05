// diagnostics.js — Line Monitor UI module
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
    // Во время анализа кадра блок порогов правил скрывается сразу.
    if (wasActive !== state.selectedAnalysisActive
        && typeof updateThresholdsPanel === 'function') {
        updateThresholdsPanel();
    }
    const live = ls.live || {};
    // fps публикуется и в live-блоке, и в jog для обратной совместимости.
    state.liveFps = Number(live.fps || (ls.jog || {}).live_fps || 0);
    state.liveStreaming = live.streaming === true;
    state.liveStatic = live.static === true;

    // При переходе между статикой и потоком (MOTION <-> CAPTURE) сразу
    // переключаем источник главного кадра: live-pull для движения без
    // геометрии, pull для стоп-кадра с правилами. Иначе оверлей прошлого
    // анализа остаётся на движущемся кадре — эффект маркера на стекле.
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
        state.viewRun = 0;
        return;
    }

    const rules = Array.isArray(report.rules) ? report.rules : [];
    // В шапке — только итоговый вердикт: решение по корпусу на этапе.
    // Для ручного анализа (SELECTED) вердикта нет — показываем сообщение.
    const verdict = frameAnalysisVerdict(report, ls) || report.message || '—';
    setIfChanged(els.frameAnalysisTitle, report.title || 'АНАЛИЗ КАДРА');
    setIfChanged(els.frameAnalysisVerdict, verdict);
    // Почему выбран именно этот прогон для картинки (ближе всего к порогу).
    if (report.picture_reason && els.frameAnalysisPicture) {
        els.frameAnalysisPicture.classList.remove('is-hidden');
        setIfChanged(
            els.frameAnalysisPicture,
            `КАРТИНКА · ПРОГОН ${report.picture_run || '—'}: ${report.picture_reason}`,
        );
    } else if (els.frameAnalysisPicture) {
        els.frameAnalysisPicture.classList.add('is-hidden');
        setIfChanged(els.frameAnalysisPicture, '');
    }
    updateFrameAnalysisFilterButtons();
    updateFrameAnalysisRulesTitle(rules);

    const renderKey = JSON.stringify({
        kind: report.kind,
        role: report.role,
        part: report.part_id,
        updated: report.updated_at,
        rules,
    });
    if (state.lastFrameAnalysisRenderKey === renderKey) {
        // Данные те же — только фильтр/прогон могли смениться снаружи.
        return;
    }
    state.lastFrameAnalysisRenderKey = renderKey;

    // Прогон картинки: при новом анализе берём серверный picture_run,
    // если оператор ещё не листает прогоны вручную.
    const pictureRun = Number(report.picture_run) || 0;
    state.frameAnalysisRulesCache = rules;
    if (!(state.runFramesAvailable >= 3 && state.viewRun >= 1)) {
        state.viewRun = pictureRun;
    }
    // Новый отчёт — по умолчанию «сработавшие», чтобы не раздувать список.
    if (state.frameAnalysisRulesFilter !== 'all'
        && state.frameAnalysisRulesFilter !== 'triggered') {
        state.frameAnalysisRulesFilter = 'triggered';
    }
    renderFrameAnalysisRules(rules, state.viewRun);
    animateUiElement(els.frameAnalysisRules, 'ui-content-change');
}

function frameAnalysisVerdict(report, ls) {
    // Однострочный итог для оператора: что произошло с корпусом на этапе.
    // Только для производственного цикла; ручной анализ кадра не трогаем.
    if (!report || report.kind !== 'CYCLE') return '';
    const rules = Array.isArray(report.rules) ? report.rules : [];
    const partId = report.part_id;

    if (partId == null) {
        // Пустой лоток: корпус не создавался, решать нечего.
        return rules.some(rule => rule.part_absent === true)
            ? 'КОРПУС НЕ ОБНАРУЖЕН'
            : '';
    }

    const parts = Array.isArray(ls.line_parts) ? ls.line_parts : [];
    const part = parts.find(p => Number(p.id) === Number(partId));
    const category = part ? String(part.category || '').toUpperCase() : '';
    const triggered = rules.some(rule => rule.triggered === true);
    const stage = String(report.stage || '').toUpperCase();

    // КОНТРОЛЬ +4: категория корпуса уже окончательная.
    if (stage.includes('КОНТРОЛЬ')) {
        if (category === 'BAD' || category === 'CLEANUP') {
            return `РЕШЕНИЕ: ${categoryLabel(category)}`;
        }
        return triggered ? 'РЕШЕНИЕ: ЕСТЬ ДЕФЕКТЫ' : 'РЕШЕНИЕ: ГОДНО';
    }

    // Стадия ВХОД: корпус принят, входные правила уже проголосовали.
    if (category === 'BAD') return 'ВХОД: РЕШЕНИЕ — БРАК';
    if (category === 'CLEANUP') return 'ВХОД: РЕШЕНИЕ — НА ОЧИСТКУ';
    if (triggered) return 'ВХОД: ЕСТЬ СРАБОТАВШИЕ ПРАВИЛА';
    return 'ВХОД: КОРПУС ПРИНЯТ, ДЕФЕКТОВ НЕТ';
}

function decisiveRules(rules) {
    // Отсутствие детали — единственная причина решения: прочие правила скрываем.
    const absent = rules.find(rule => rule.part_absent === true);
    if (absent) return [absent];
    return rules;
}

// Правила, влияющие на решение: сработавшие / пропуск / нет корпуса.
function triggeredRules(rules) {
    const base = decisiveRules(rules);
    const filtered = base.filter(rule => (
        rule.part_absent === true
        || rule.triggered === true
        || rule.skipped === true
    ));
    // Если все в норме — показываем полный список, иначе пустой фильтр
    // выглядел бы как «анализа нет».
    return filtered.length ? filtered : base;
}

function visibleFrameAnalysisRules(rules) {
    const base = decisiveRules(rules);
    if (state.frameAnalysisRulesFilter === 'all') return base;
    return triggeredRules(rules);
}

function updateFrameAnalysisRulesTitle(rules) {
    if (!els.frameAnalysisRulesTitle) return;
    const all = decisiveRules(rules || []);
    const bad = triggeredRules(rules || []);
    const showing = state.frameAnalysisRulesFilter === 'all' ? all : bad;
    // Если фильтр «сработавшие», а брака нет — bad === all (fallback).
    const label = state.frameAnalysisRulesFilter === 'all'
        ? `ПРАВИЛА · ${showing.length}`
        : (bad.length === all.length && !all.some(r => r.triggered || r.part_absent || r.skipped)
            ? `ПРАВИЛА · ${showing.length}`
            : `ПРАВИЛА · ${showing.length}/${all.length}`);
    setIfChanged(els.frameAnalysisRulesTitle, label);
}

function updateFrameAnalysisFilterButtons() {
    const filter = state.frameAnalysisRulesFilter === 'all' ? 'all' : 'triggered';
    if (els.frameAnalysisFilterTriggered) {
        const on = filter === 'triggered';
        els.frameAnalysisFilterTriggered.classList.toggle('is-active', on);
        els.frameAnalysisFilterTriggered.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
    if (els.frameAnalysisFilterAll) {
        const on = filter === 'all';
        els.frameAnalysisFilterAll.classList.toggle('is-active', on);
        els.frameAnalysisFilterAll.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
}

function setFrameAnalysisRulesFilter(next) {
    const value = next === 'all' ? 'all' : 'triggered';
    if (state.frameAnalysisRulesFilter === value) return;
    state.frameAnalysisRulesFilter = value;
    updateFrameAnalysisFilterButtons();
    if (state.frameAnalysisRulesCache) {
        updateFrameAnalysisRulesTitle(state.frameAnalysisRulesCache);
        renderFrameAnalysisRules(state.frameAnalysisRulesCache, state.viewRun);
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

function renderFrameAnalysisRules(rules, pictureRun) {
    els.frameAnalysisRules.replaceChildren();
    updateFrameAnalysisFilterButtons();
    updateFrameAnalysisRulesTitle(rules);
    if (!rules.length) {
        appendFrameAnalysisEmpty(
            els.frameAnalysisRules,
            'Ожидание результатов правил',
        );
        return;
    }
    const visibleRules = visibleFrameAnalysisRules(rules);
    if (!visibleRules.length) {
        appendFrameAnalysisEmpty(
            els.frameAnalysisRules,
            state.frameAnalysisRulesFilter === 'triggered'
                ? 'Сработавших правил нет'
                : 'Ожидание результатов правил',
        );
        return;
    }
    for (const rule of visibleRules) {
        els.frameAnalysisRules.appendChild(
            buildFrameAnalysisRuleGroup(rule, pictureRun)
        );
    }
}

// Правило → секция в стиле «Порогов правил»: заголовок (название +
// статус), затем пороги с тремя замерами. Каждый порог — read-only поле
// (предел) и под ним три замера с прогонов голосования 2 из 3.
function buildFrameAnalysisRuleGroup(rule, pictureRun) {
    const stateClass = rule.neutral
        ? ''
        : (rule.skipped
            ? 'skipped'
            : (rule.triggered ? 'triggered' : 'ok'));
    const group = document.createElement('section');
    group.className = `fa-group ${stateClass}`.trim();
    if (rule.part_absent) group.classList.add('part-absent');

    const head = document.createElement('div');
    head.className = 'fa-group-head';
    const name = document.createElement('span');
    name.className = 'fa-group-name';
    name.textContent = rule.name || 'Без названия';
    head.appendChild(name);
    const result = document.createElement('b');
    result.className = 'fa-group-status';
    result.textContent = rule.status_label || (
        rule.skipped
            ? 'НЕ ВЫПОЛНЕНО'
            : (rule.triggered ? 'СРАБОТАЛО' : 'НОРМА')
    );
    head.appendChild(result);
    group.appendChild(head);

    if (rule.part_absent) {
        const absent = document.createElement('div');
        absent.className = 'frame-analysis-human-cause';
        absent.textContent = 'КОРПУС НЕ ОБНАРУЖЕН';
        group.appendChild(absent);
        group.classList.add('has-human-cause');
    } else if (rule.triggered && rule.human_cause) {
        const cause = document.createElement('div');
        cause.className = 'frame-analysis-human-cause';
        cause.textContent = rule.human_cause;
        group.appendChild(cause);
        group.classList.add('has-human-cause');
    }

    // Пороги + три замера; решающий порог выделен в rule-summary.js.
    const measurements = renderRuleMeasurements(rule, pictureRun);
    if (measurements.children.length) {
        group.classList.add('has-detail');
        group.appendChild(measurements);
    } else {
        const summary = ruleSummaryLines(rule);
        const showSummary = (
            rule.triggered || rule.skipped || rule.show_detail || rule.part_absent
        );
        if (summary.length && showSummary) {
            group.classList.add('has-detail');
            for (const line of summary) {
                const reason = document.createElement('small');
                reason.className = 'frame-analysis-reason';
                reason.textContent = line;
                group.appendChild(reason);
            }
        }
    }
    return group;
}

function appendFrameAnalysisEmpty(container, text) {
    const item = document.createElement('div');
    item.className = 'frame-analysis-empty';
    item.textContent = text;
    container.appendChild(item);
}

// Клик по замеру → тот же прогон на главной камере.
function setupFrameAnalysisRunClicks() {
    const list = els.frameAnalysisRules;
    if (!list || list.dataset.runClicksBound === '1') return;
    list.dataset.runClicksBound = '1';
    const activate = (event) => {
        const chip = event.target.closest('.fa-measurement-value[data-run]');
        if (!chip || !list.contains(chip)) return;
        event.preventDefault();
        event.stopPropagation();
        const run = Number(chip.dataset.run);
        if (!run) return;
        if (typeof setMainCameraRun === 'function') {
            setMainCameraRun(run);
        }
    };
    list.addEventListener('click', activate);
    list.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        activate(event);
    });
}

function setupFrameAnalysisFilter() {
    if (els.frameAnalysisFilterTriggered) {
        els.frameAnalysisFilterTriggered.addEventListener('click', () => {
            setFrameAnalysisRulesFilter('triggered');
        });
    }
    if (els.frameAnalysisFilterAll) {
        els.frameAnalysisFilterAll.addEventListener('click', () => {
            setFrameAnalysisRulesFilter('all');
        });
    }
    updateFrameAnalysisFilterButtons();
}

function showPendingSelectedFrameAnalysis() {
    if (!els.frameAnalysisPanel) return;
    state.frameAnalysisRulesCache = null;
    state.viewRun = 0;
    els.frameAnalysisPanel.classList.remove('is-collapsed');
    if (els.statsSummary) els.statsSummary.classList.add('is-collapsed');
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.classList.add('is-collapsed');
    }
    if (els.statsService) els.statsService.classList.add('is-collapsed');
    setIfChanged(els.frameAnalysisTitle, 'АНАЛИЗ КАДРА');
    setIfChanged(els.frameAnalysisVerdict, 'Подготовка моделей и правил');
    setIfChanged(els.frameAnalysisRulesTitle, 'ПРАВИЛА');
    els.frameAnalysisRules.replaceChildren();
    appendFrameAnalysisEmpty(els.frameAnalysisRules, 'Ожидание результатов');
}

function setupSelectedFrameAnalysis() {
    setupFrameAnalysisRunClicks();
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
                    `/api/diagnostics/selected/${encodeURIComponent(role)}`,
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
