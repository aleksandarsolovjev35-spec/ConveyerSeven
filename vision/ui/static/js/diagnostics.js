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

    const models = Array.isArray(report.models) ? report.models : [];
    const rules = Array.isArray(report.rules) ? report.rules : [];
    // Контекст панели: этап линии, выбранная оператором камера и корпус.
    // Этап не дублируется, если уже прозвучал в названии камеры
    // (например, «ВХОД» + «ВХОД · СЛЕВА» → «ВХОД · СЛЕВА»).
    const contextBits = [];
    const stageText = report.stage ? String(report.stage) : '';
    const roleText = report.role ? cameraRoleLabel(report.role) : '';
    const stageFirst = String(stageText.split(' ')[0] || '');
    const roleFirst = String(roleText.split(' ')[0] || '');
    if (stageText && roleFirst !== stageFirst) contextBits.push(stageText);
    if (roleText) contextBits.push(roleText);
    if (report.part_id) contextBits.push(`КОРПУС #${report.part_id}`);
    const context = contextBits.length
        ? contextBits.join(' · ')
        : 'ТЕКУЩИЙ ЦИКЛ';
    setIfChanged(els.frameAnalysisTitle, report.title || 'АНАЛИЗ КАДРА');
    setIfChanged(els.frameAnalysisContext, context);
    setIfChanged(
        els.frameAnalysisMessage,
        frameAnalysisVerdict(report, ls)
            || report.message
            || 'Ожидание результатов анализа',
    );
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

    // Прогон, по которому построена картинка (кадр с разметкой): его
    // замеры в строках «три замера порога» помечаются рамкой. При новом
    // анализе показываем выбранный сервером прогон; по клику на главный
    // кадр оператор переключает его (state.viewRun).
    const pictureRun = Number(report.picture_run) || 0;
    state.frameAnalysisRulesCache = rules;
    // Порядок показа кадров — всегда хронологический. picture_run нужен
    // только для метаданных/подсветки, но не должен перескакивать картинку
    // сразу на наиболее согласованный прогон.
    if (!(state.runFramesAvailable >= 3 && state.viewRun >= 1)) {
        state.viewRun = pictureRun;
    }
    renderFrameAnalysisModels(models);
    renderFrameAnalysisRules(rules, state.viewRun);
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
        // Показываем обнаружения по всем трём прогонам (2/2/1), а не один
        // выбранный результат, чтобы оператор видел разброс по кадрам.
        result.textContent = model.ok
            ? `${latencyLabel} · объекты ${detectionsByRun}`
            : 'ОШИБКА';
        if (model.error) item.title = String(model.error);
        item.append(name, result);
        els.frameAnalysisModels.appendChild(item);
    }
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

        // Компактно: под правилом — все пороги с тремя замерами по прогонам
        // (значение выбранного для картинки прогона помечается рамкой).
        const measurements = renderRuleMeasurements(rule, pictureRun);
        if (measurements.children.length) {
            item.classList.add('has-detail');
            item.appendChild(measurements);
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

// Блок «какие модели сработали» свёрнут по умолчанию; по клику на заголовок
// его можно развернуть и уточнить детали.
function setupFrameAnalysisModelsCollapse() {
    const toggle = els.frameAnalysisModelsToggle;
    const list = els.frameAnalysisModels;
    if (!toggle || !list) return;
    toggle.addEventListener('click', () => {
        const collapsed = list.classList.toggle('frame-analysis-list-collapsed');
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    });
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
    setupFrameAnalysisModelsCollapse();
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
