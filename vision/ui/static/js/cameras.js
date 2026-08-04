// cameras.js — Line Monitor UI module
'use strict';

// ─── Camera hover ────────────────────────────────────────────

function setupCameraHover() {
    const container = els.cameraContainer;
    if (!container) return;

    container.addEventListener('mouseenter', () => {
        state.cameraHovered = true;
        if (els.cameraOverlay.dataset.peekable === '1') {
            els.cameraOverlay.classList.add('overlay-peek');
        }
    });

    container.addEventListener('mouseleave', () => {
        state.cameraHovered = false;
        els.cameraOverlay.classList.remove('overlay-peek');
    });
}

// ─── Переключение прогонов на главной камере ───────────────────

// Доступность: три кадра прогонов есть только у статичного анализа
// (лента стоит). Подсказка курсором и tooltip'ом появляется в этом случае.
function updateRunCycleAvailability() {
    const container = els.cameraContainer;
    if (!container) return;
    const available = state.runFramesAvailable >= 3 && !state.splashActive;
    container.classList.toggle('run-cyclable', available);
    container.title = available
        ? 'Клик — кадры трёх прогонов анализа'
        : '';
}

function cycleMainCameraRun() {
    if (state.splashActive || state.offline) return;
    // Переключение прогонов относится только к статичным кадрам анализа:
    // замороженный кадр выбранной камеры или стоп-кадр рабочего цикла.
    // Во время движения (поток) или JOG-хода кадры прогонов неактуальны.
    const staticShown = state.selectedAnalysisActive
        || (!state.liveStreaming && state.liveStatic);
    if (!staticShown) return;
    if (state.runFramesAvailable < 3) return;

    state.viewRun = (state.viewRun % state.runFramesAvailable) + 1;

    clearLivePullTimer();
    if (state.selectedAnalysisActive) {
        // Замороженный кадр анализа: перезапрашиваем с новым run.
        showSelectedAnalysisFrame(
            state.selectedAnalysisRole || state.currentCamera,
        );
    } else {
        state.mainCamMode = 'pull';
        state.mainCamStreamRole = null;
        state.mainCamStreamView = null;
        mainBufferLoading = false;
        maybeRequestMainFrame();
    }

    if (typeof applyLiveBadge === 'function') applyLiveBadge(state.jogActive);
    // Рамка выбранного замера следует за просматриваемым прогоном.
    if (
        typeof renderFrameAnalysisRules === 'function'
        && state.frameAnalysisRulesCache
    ) {
        renderFrameAnalysisRules(state.frameAnalysisRulesCache, state.viewRun);
    }
}

function setupMainCameraRunCycle() {
    const container = els.cameraContainer;
    if (!container) return;
    container.addEventListener('click', (event) => {
        if (event.target.closest('button, input, select, a')) return;
        if (event.target.closest('.camera-live-controls')) return;
        cycleMainCameraRun();
    });
    updateRunCycleAvailability();
}

// ─── Mode ────────────────────────────────────────────────────

function updateMode(mode) {
    const normalized = mode === 'RAW' ? 'RAW' : 'RULES';
    const changed = state.mode !== normalized;
    state.mode = normalized;
    applyModeUI(normalized);
    if (changed) applyMainCameraSource();
}

function viewModeContextVisible() {
    const production = ['RUNNING', 'STOPPING'].includes(state.lineState);
    const selectedFrame = (
        JOG_ALLOWED_STATES.includes(state.lineState)
        && state.selectedAnalysisActive
    );
    return production || selectedFrame;
}

function viewModeAllowed() {
    return (
        viewModeContextVisible()
        && state.statusReceived
        && Object.keys(state.backendControls || {}).length > 0
        && !state.controlPending
        && !state.offline
        && !state.modePending
        && !state.jogBusy
        && !state.distributorDiagnosticPending
        && !state.distributorDiagnosticBackendBusy
        && !state.selectedAnalysisPending
    );
}

function updateViewModeControls() {
    const visible = viewModeContextVisible() && !state.offline;
    const allowed = viewModeAllowed();
    if (els.viewModeToggle) {
        els.viewModeToggle.classList.toggle('is-faded', !visible);
        els.viewModeToggle.disabled = !allowed;
        els.viewModeToggle.textContent = state.mode === 'RULES'
            ? 'ВИД: ПРАВИЛА'
            : 'ВИД: RAW';
        els.viewModeToggle.setAttribute(
            'aria-pressed',
            state.mode === 'RULES' ? 'true' : 'false',
        );
    }
}

function applyModeUI() {
    updateViewModeControls();
    // Единственный источник истины для бейджа — applyLiveBadge: поток,
    // стоп-кадр, анализ или скрытие. Иначе is-faded, добавленный здесь на
    // каждом тике updateMode(status.mode), перекрывал видимый бейдж,
    // который только что выставила applyLiveBadge в updateLineStatus.
    if (typeof applyLiveBadge === 'function') {
        applyLiveBadge(state.jogActive);
        return;
    }
    if (state.jogActive || state.selectedAnalysisActive) return;
    els.modeBadge.classList.add('is-faded');
}

async function setViewMode(newMode) {
    if (!viewModeAllowed()) return;
    if (newMode !== 'RAW' && newMode !== 'RULES') return;
    if (state.mode === newMode) return;

    const oldMode = state.mode;
    state.modePending = true;
    updateViewModeControls();
    clearControlError();
    try {
        const result = await apiPost(`/api/mode/${newMode}`, true);
        if (!result) {
            applyModeUI(oldMode);
            return;
        }
        state.mode = newMode;
        if (typeof result.frame_version === 'number') {
            state.currentVersion = result.frame_version;
        }
        state.mainCamStreamView = null;
        applyModeUI(newMode);
        applyMainCameraSource();
        refreshPreviewStrip();
        requestImmediateStatus();
    } finally {
        state.modePending = false;
        updateViewModeControls();
    }
}

async function toggleMode() {
    const newMode = state.mode === 'RULES' ? 'RAW' : 'RULES';
    await setViewMode(newMode);
}

function setupViewModeControls() {
    if (!els.viewModeToggle) return;
    els.viewModeToggle.addEventListener('click', toggleMode);
    updateViewModeControls();
}

// ─── Cameras ─────────────────────────────────────────────────

async function fetchCameras() {
    if (!state.bootDone || state.camerasFetchBusy) return;
    state.camerasFetchBusy = true;
    const data = await apiGet('/api/cameras');
    state.camerasFetchBusy = false;
    if (!data || !data.cameras || !data.cameras.length) return;

    const changed = (
        state.cameras.length !== data.cameras.length
        || state.cameras.some((c, i) => c !== data.cameras[i])
    );
    if (!changed) return;

    state.cameras = data.cameras;

    if (
        !state.currentCamera
        || !state.cameras.includes(state.currentCamera)
    ) {
        state.currentCamera = state.cameras[0];
        sendActiveCameraIfChanged(state.currentCamera);
    }

    renderPreviewStrip();
    updateMainCameraLabel();

    if (!state.splashActive) {
        applyMainCameraSource();
    }

    if (typeof updateThresholdsPanel === 'function') {
        updateThresholdsPanel();
    }

    checkUiReady();
}

function renderPreviewStrip() {
    els.previewStrip.innerHTML = state.cameras.map((role, i) => `
        <div class="preview-cam ${role === state.currentCamera ? 'active' : ''}"
             data-role="${role}"
             data-index="${i}">
            <img src="/frame/${role}?mode=${state.mode}&preview=1&t=${Date.now()}"
                 alt="${cameraRoleLabel(role)}">
            <div class="preview-cam-label">[${i + 1}] ${cameraRoleLabel(role)}</div>
        </div>
    `).join('');

    els.previewStrip.querySelectorAll('.preview-cam').forEach(el => {
        el.addEventListener('click', () => {
            selectCamera(el.dataset.role);
        });
    });
}

function selectCamera(role) {
    if (state.selectedAnalysisActive || state.selectedAnalysisPending) return;
    if (!state.cameras.includes(role)) return;
    if (state.currentCamera === role) return;

    state.currentCamera = role;

    els.previewStrip.querySelectorAll('.preview-cam').forEach(el => {
        el.classList.toggle('active', el.dataset.role === role);
    });

    updateMainCameraLabel();
    // Панель анализа кадра следует за выбранной камерой: статус
    // опрашивается сразу и ещё раз после того, как backend зафиксировал
    // новую активную камеру.
    sendActiveCameraIfChanged(role).finally(requestImmediateStatus);
    // Панель порогов правил тоже следует за выбранной (главной) камерой.
    if (typeof updateThresholdsPanel === 'function') {
        updateThresholdsPanel();
    }
    applyMainCameraSource();
    requestImmediateStatus();
}

function navigateCamera(direction) {
    if (!state.cameras.length) return;

    const idx  = state.cameras.indexOf(state.currentCamera);
    const next = (idx + direction + state.cameras.length)
                 % state.cameras.length;
    selectCamera(state.cameras[next]);
}

function updateMainCameraLabel() {
    setIfChanged(els.cameraLabel, cameraRoleLabel(state.currentCamera));
}

function showSelectedAnalysisFrame(role) {
    if (!role) return;
    setIfChanged(els.cameraLabel, `${cameraRoleLabel(role)} · АНАЛИЗ`);
    clearLivePullTimer();
    const run = (
        state.viewRun > 0 && state.runFramesAvailable >= 3
    ) ? state.viewRun : 0;
    const analysisKey = `${role}|${state.mode}|${state.currentVersion}|${run}`;
    if (
        state.mainCamMode === 'analysis'
        && state.mainCamAnalysisKey === analysisKey
    ) {
        return;
    }
    state.mainCamMode = 'analysis';
    state.mainCamStreamRole = null;
    state.mainCamStreamView = null;
    state.mainCamAnalysisKey = analysisKey;
    mainBufferLoading = false;
    els.mainCamera.src = (
        `/frame/${encodeURIComponent(role)}`
        + `?mode=${encodeURIComponent(state.mode)}`
        + `&v=${state.currentVersion}&analysis=1`
        + (run ? `&run=${run}` : '')
    );
}

function returnSelectedCameraToLive() {
    updateMainCameraLabel();
    state.mainCamMode = 'pull';
    state.mainCamStreamRole = null;
    state.mainCamStreamView = null;
    state.mainCamAnalysisKey = null;
    mainBufferLoading = false;
    els.mainCamera.removeAttribute('src');
    applyMainCameraSource();
}

// ─── Main camera source switch ──────────────────────────────

function clearLivePullTimer() {
    if (state.livePullTimer) {
        clearTimeout(state.livePullTimer);
        state.livePullTimer = null;
    }
}

function scheduleNextLiveFrame(delay = LIVE_CAM_MIN_GAP) {
    clearLivePullTimer();
    if (state.mainCamMode !== 'live-pull') return;
    state.livePullTimer = setTimeout(() => {
        state.livePullTimer = null;
        maybeRequestMainFrame();
    }, Math.max(1, delay));
}

function applyMainCameraSource() {
    if (state.selectedAnalysisActive) {
        clearLivePullTimer();
        showSelectedAnalysisFrame(state.selectedAnalysisRole || state.currentCamera);
        return;
    }
    if (!state.currentCamera) return;
    if (state.splashActive) return;

    // Во время любого движения (JOG или производственное MOTION) оператор
    // должен видеть поток без геометрии. Геометрия построена по статичному
    // кадру и на движущемся изображении указывала бы мимо детали — эффект
    // маркера на стекле. Поэтому используем live-pull и во время RUNNING.
    const shouldLivePull = state.jogActive || state.liveStreaming;

    if (shouldLivePull) {
        const desiredRole = state.currentCamera;
        const desiredView = state.mode;
        if (
            state.mainCamMode === 'live-pull'
            && state.mainCamStreamRole === desiredRole
            && state.mainCamStreamView === desiredView
        ) {
            maybeRequestMainFrame();
            return;
        }

        clearLivePullTimer();
        state.mainCamMode       = 'live-pull';
        state.mainCamStreamRole = desiredRole;
        state.mainCamStreamView = desiredView;
        state.mainCamAnalysisKey = null;
        mainBufferLoading = false;
        maybeRequestMainFrame();
    } else {
        clearLivePullTimer();
        if (state.mainCamMode === 'pull') {
            maybeRequestMainFrame();
            return;
        }

        state.mainCamMode       = 'pull';
        state.mainCamStreamRole = null;
        state.mainCamStreamView = null;
        state.mainCamAnalysisKey = null;

        els.mainCamera.removeAttribute('src');
        maybeRequestMainFrame();
    }
}

function maybeRequestMainFrame() {
    if (state.mainCamMode !== 'pull' && state.mainCamMode !== 'live-pull') return;
    if (mainBufferLoading) return;
    if (!state.currentCamera) return;
    if (state.splashActive) return;

    const now = Date.now();
    const gap = now - state.lastFrameTime;
    const minimumGap = state.mainCamMode === 'live-pull'
        ? LIVE_CAM_MIN_GAP
        : MAIN_CAM_MIN_GAP;

    if (gap < minimumGap) {
        if (state.mainCamMode === 'live-pull') {
            scheduleNextLiveFrame(minimumGap - gap);
        } else {
            setTimeout(maybeRequestMainFrame, minimumGap - gap);
        }
        return;
    }

    state.lastFrameTime   = now;
    state.lastSeenVersion = state.currentVersion;
    mainBufferLoading     = true;
    mainBufferRequestRole = state.currentCamera;
    mainBufferRequestView = state.mode;
    mainBufferRequestVersion = state.currentVersion;

    const versionQuery = state.mainCamMode === 'live-pull'
        ? `live=1&t=${Date.now()}`
        : `v=${state.currentVersion}`;
    // В статичном режиме добавляем номер прогона: по клику на главный кадр
    // оператор переключает кадры трёх прогонов голосования 2 из 3.
    const runQuery = (
        state.mainCamMode === 'pull'
        && state.viewRun > 0
        && state.runFramesAvailable >= 3
    ) ? `&run=${state.viewRun}` : '';
    mainBuffer.src =
        `/frame/${state.currentCamera}`
        + `?mode=${state.mode}&${versionQuery}${runQuery}`;
}

function refreshPreviewStrip() {
    if (state.splashActive) return;

    els.previewStrip.querySelectorAll('.preview-cam img').forEach(img => {
        if (img.dataset.refreshing === '1') return;
        const role = img.parentElement.dataset.role;
        const roleVersion = Number(state.frameVersions[role] || 0);
        const frameKey = `${roleVersion}|${state.mode}`;
        if (img.dataset.frameKey === frameKey) return;
        if (img.dataset.requestedKey === frameKey) return;

        const tmp = new Image();
        img.dataset.refreshing = '1';
        img.dataset.requestedKey = frameKey;

        tmp.onload = () => {
            img.src = tmp.src;
            img.dataset.frameKey = frameKey;
            img.dataset.requestedKey = '';
            img.dataset.refreshing = '0';
        };
        tmp.onerror = () => {
            img.dataset.requestedKey = '';
            img.dataset.refreshing = '0';
        };
        tmp.src =
            `/frame/${role}`
            + `?mode=${state.mode}&preview=1&rv=${roleVersion}`;
    });
}

// ─── Uptime ──────────────────────────────────────────────────

function updateUptime() {
    const elapsed = Math.floor((Date.now() - state.startTime) / 1000);
    const h = String(Math.floor(elapsed / 3600)).padStart(2, '0');
    const m = String(Math.floor((elapsed % 3600) / 60)).padStart(2, '0');
    const s = String(elapsed % 60).padStart(2, '0');
    setIfChanged(els.metricUptime, `${h}:${m}:${s}`);
}

