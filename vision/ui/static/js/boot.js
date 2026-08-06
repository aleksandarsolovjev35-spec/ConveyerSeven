// boot.js — стабилизированный splash
'use strict';

let _bootSeq = 0;

async function fetchBoot() {
    if (state.bootDone) return;
    if (state.bootFetchBusy) return;
    const mySeq = ++_bootSeq;
    state.bootFetchBusy = true;
    const boot = await apiGet('/api/boot');
    state.bootFetchBusy = false;
    if (mySeq !== _bootSeq) return; // устаревший
    if (!boot) return;

    const pct = Math.round((boot.progress || 0) * 100);
    if (els.splashProgress) els.splashProgress.style.width = `${pct}%`;

    if (!state.bootDone) setIfChanged(els.splashMessage, boot.message || 'Загрузка');

    if (boot.error) {
        if (els.splashError) els.splashError.classList.remove('is-hidden');
        setIfChanged(els.splashErrorMsg, boot.error);
    }

    if (!boot.active && !state.bootDone) {
        state.bootDone = true;
        state.bootDoneAt = Date.now();
        if (state.bootInterval) { clearInterval(state.bootInterval); state.bootInterval = null; }
        console.log('[BOOT] Backend ready, waiting for UI data...');
        if (els.splashProgress) els.splashProgress.style.width = '100%';
        startStatusPolling();
        fetchCameras();
    }
}

function checkUiReady() {
    if (state.uiRevealed) return;
    if (!state.bootDone) return;
    const timeSinceBoot = Date.now() - state.bootDoneAt;
    const timedOut = timeSinceBoot > UI_READY_TIMEOUT;
    const ready = state.statusReceived && state.jogReceived && state.cameras.length > 0 && state.currentCamera !== null;
    if (!ready && !timedOut) updateSplashWaitingMessage();
    if (ready || timedOut) {
        state.uiReady = true;
        if (timedOut && !ready) {
            console.warn('[UI] Ready timeout after boot — showing UI anyway.', {
                statusReceived: state.statusReceived,
                jogReceived: state.jogReceived,
                cameras: state.cameras.length,
                currentCamera: state.currentCamera,
            });
        } else {
            console.log('[UI] Ready — showing main UI');
        }
        revealUi();
    }
}

function updateSplashWaitingMessage() {
    const missing = [];
    if (!state.statusReceived) missing.push('состояние системы');
    if (!state.jogReceived) missing.push('ручное управление');
    if (state.cameras.length === 0) missing.push('камеры');
    if (state.currentCamera === null) missing.push('выбор камеры');
    if (missing.length > 0) setIfChanged(els.splashMessage, `Ожидание: ${missing.join(', ')}`);
    else setIfChanged(els.splashMessage, 'Почти готово');
}

function startUiReadyWatcher() {
    const interval = setInterval(() => {
        checkUiReady();
        if (state.uiRevealed) clearInterval(interval);
    }, UI_READY_CHECK_INT);
}

function revealUi() {
    if (state.uiRevealed) return;
    state.uiRevealed = true;
    setIfChanged(els.splashMessage, 'Готово');
    setTimeout(() => {
        if (els.splash) els.splash.classList.add('splash-fadeout');
        setTimeout(() => {
            if (els.splash) els.splash.classList.add('is-hidden');
            if (els.main) els.main.classList.remove('is-hidden');
            state.splashActive = false;
            applyMainCameraSource();
            // После показа UI — сразу синхронизируем скроллы
            if (typeof faSyncScroll === 'function') requestAnimationFrame(() => faSyncScroll());
            if (typeof thresholdsSyncScroll === 'function') requestAnimationFrame(() => thresholdsSyncScroll());
        }, 400);
    }, 200);
}
