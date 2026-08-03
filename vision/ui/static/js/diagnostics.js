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
        return;
    }

    const models = Array.isArray(report.models) ? report.models : [];
    const rules = Array.isArray(report.rules) ? report.rules : [];
    // Контекст панели: этап линии, выбранная оператором камера и деталь.
    const contextBits = [];
    if (report.stage) contextBits.push(String(report.stage));
    if (report.role) contextBits.push(cameraRoleLabel(report.role));
    if (report.part_id) contextBits.push(`КОРПУС #${report.part_id}`);
    const context = contextBits.length
        ? contextBits.join(' · ')
        : 'ТЕКУЩИЙ ЦИКЛ';
    setIfChanged(els.frameAnalysisTitle, report.title || 'АНАЛИЗ КАДРА');
    setIfChanged(els.frameAnalysisContext, context);
    setIfChanged(
        els.frameAnalysisMessage,
        report.message || 'Ожидание результатов анализа',
    );
    setIfChanged(els.frameAnalysisModelsTitle, `МОДЕЛИ · ${models.length}`);
    const decisive = decisiveRules(rules);
    setIfChanged(
        els.frameAnalysisRulesTitle,
        decisive.length === rules.length
            ? `ПРАВИЛА · ${rules.length}`
            : `ПРАВИЛА · ${decisive.length}/${rules.length}`,
    );

    const renderKey = JSON.stringify({
        kind: report.kind,
        role: report.role,
        part: report.part_id,
        updated: report.updated_at,
        models,
        rules,
    });
    if (state.lastFrameAnalysisRenderKey === renderKey) return;
    state.lastFrameAnalysisRenderKey = renderKey;

    renderFrameAnalysisModels(models);
    renderFrameAnalysisRules(rules);
    animateUiElement(els.frameAnalysisModels, 'ui-content-change');
    animateUiElement(els.frameAnalysisRules, 'ui-content-change');
}

function renderFrameAnalysisModels(models) {
    els.frameAnalysisModels.replaceChildren();
    if (!models.length) {
        appendFrameAnalysisEmpty(
            els.frameAnalysisModels,
            'Ожидание результатов моделей',
        );
        return;
    }
    for (const model of models) {
        const item = document.createElement('div');
        item.className = `frame-analysis-item ${model.ok ? 'ok' : 'error'}`;
        const name = document.createElement('span');
        const result = document.createElement('b');
        const fileName = String(model.model || '').split('/').pop();
        const runCount = Number(model.runs || 0);
        const runLabel = runCount > 1 ? `${runCount} ПРОГОНА · ` : '';
        name.textContent = `${runLabel}${cameraRoleLabel(model.role)} · ${fileName}`;
        const detectionsByRun = Array.isArray(model.detections_by_run)
            ? model.detections_by_run.join('/')
            : String(model.detections || 0);
        const latencyLabel = runCount > 1
            ? `${Number(model.elapsed_ms || 0).toFixed(0)} мс ср.`
            : `${Number(model.elapsed_ms || 0).toFixed(0)} мс`;
        result.textContent = model.ok
            ? `${latencyLabel} · ${detectionsByRun}`
            : 'ОШИБКА';
        if (model.error) item.title = String(model.error);
        item.append(name, result);
        els.frameAnalysisModels.appendChild(item);
    }
}

function decisiveRules(rules) {
    // Отсутствие детали — единственная причина решения: прочие правила скрываем.
    const absent = rules.find(rule => rule.part_absent === true);
    if (absent) return [absent];
    return rules;
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

function renderFrameAnalysisRules(rules) {
    els.frameAnalysisRules.replaceChildren();
    if (!rules.length) {
        appendFrameAnalysisEmpty(
            els.frameAnalysisRules,
            'Ожидание результатов правил',
        );
        return;
    }
    const visibleRules = decisiveRules(rules);
    for (const rule of visibleRules) {
        const item = document.createElement('div');
        const stateClass = rule.neutral
            ? ''
            : (rule.skipped
                ? 'skipped'
                : (rule.triggered ? 'triggered' : 'ok'));
        item.className = `frame-analysis-item ${stateClass}`.trim();
        if (rule.part_absent) item.classList.add('part-absent');

        const name = document.createElement('span');
        const result = document.createElement('b');
        name.textContent = rule.name || 'Без названия';
        result.textContent = rule.status_label || (
            rule.skipped
                ? 'НЕ ВЫПОЛНЕНО'
                : (rule.triggered ? 'СРАБОТАЛО' : 'НОРМА')
        );
        item.append(name, result);

        if (rule.part_absent) {
            const absent = document.createElement('div');
            absent.className = 'frame-analysis-human-cause';
            absent.textContent = 'КОРПУС НЕ ОБНАРУЖЕН';
            item.appendChild(absent);
            item.classList.add('has-human-cause');
        } else if (rule.triggered && rule.human_cause) {
            const cause = document.createElement('div');
            cause.className = 'frame-analysis-human-cause';
            cause.textContent = rule.human_cause;
            item.appendChild(cause);
            item.classList.add('has-human-cause');
        }

        // Наглядная сводка: что обнаружено и какие получились показатели.
        const cards = Array.isArray(rule.summary_cards) ? rule.summary_cards : [];
        if (cards.length) {
            item.classList.add('has-detail');
            item.appendChild(renderRuleSummaryCards(cards));
        } else {
            const summary = ruleSummaryLines(rule);
            const showSummary = (
                rule.triggered || rule.skipped || rule.show_detail || rule.part_absent
            );
            if (summary.length && showSummary) {
                item.classList.add('has-detail');
                for (const line of summary) {
                    const reason = document.createElement('small');
                    reason.className = 'frame-analysis-reason';
                    reason.textContent = line;
                    item.appendChild(reason);
                }
            }
        }

        els.frameAnalysisRules.appendChild(item);
    }
}

function appendFrameAnalysisEmpty(container, text) {
    const item = document.createElement('div');
    item.className = 'frame-analysis-empty';
    item.textContent = text;
    container.appendChild(item);
}

function showPendingSelectedFrameAnalysis() {
    if (!els.frameAnalysisPanel) return;
    els.frameAnalysisPanel.classList.remove('is-collapsed');
    if (els.statsSummary) els.statsSummary.classList.add('is-collapsed');
    if (els.distributorDiagnostics) {
        els.distributorDiagnostics.classList.add('is-collapsed');
    }
    if (els.statsService) els.statsService.classList.add('is-collapsed');
    setIfChanged(els.frameAnalysisTitle, 'АНАЛИЗ КАДРА');
    setIfChanged(els.frameAnalysisContext, cameraRoleLabel(state.currentCamera));
    setIfChanged(els.frameAnalysisMessage, 'Подготовка моделей и правил');
    setIfChanged(els.frameAnalysisModelsTitle, 'МОДЕЛИ');
    setIfChanged(els.frameAnalysisRulesTitle, 'ПРАВИЛА');
    els.frameAnalysisModels.replaceChildren();
    els.frameAnalysisRules.replaceChildren();
    appendFrameAnalysisEmpty(els.frameAnalysisModels, 'Запуск моделей');
    appendFrameAnalysisEmpty(els.frameAnalysisRules, 'Ожидание результатов');
}

function setupSelectedFrameAnalysis() {
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
