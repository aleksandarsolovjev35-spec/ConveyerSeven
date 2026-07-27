// bootstrap.js — Line Monitor UI module
'use strict';

// ─── Hotkeys ─────────────────────────────────────────────────

function setupHotkeys() {
    window.addEventListener('keydown', (e) => {
        const tag = (e.target && e.target.tagName) || '';
        const inInput = tag === 'INPUT' || tag === 'TEXTAREA';

        if (e.key === 'Escape') {
            const fullscreen = document.querySelector('.gallery-fullscreen');
            if (fullscreen) {
                fullscreen.remove();
                e.preventDefault();
                return;
            }

            if (
                els.galleryModal
                && !els.galleryModal.classList.contains('is-hidden')
            ) {
                closeGallery();
                e.preventDefault();
                return;
            }

            if (inInput) return;

            els.btnExit.click();
            return;
        }

        if (inInput) return;

        if (e.key === 'F5') {
            e.preventDefault();
            els.btnStart.click();
            return;
        }

        if (e.key === 'F6') {
            e.preventDefault();
            els.btnStop.click();
            return;
        }

        if (e.key === 'F11') {
            e.preventDefault();
            if (
                !document.fullscreenElement
                && typeof document.documentElement.requestFullscreen === 'function'
            ) {
                document.documentElement.requestFullscreen().catch(() => {});
            } else if (
                document.fullscreenElement
                && typeof document.exitFullscreen === 'function'
            ) {
                document.exitFullscreen().catch(() => {});
            }
            return;
        }

        if (state.jogActive) {
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                e.preventDefault();
                if (!e.repeat && !state.jogHoldDirection) {
                    const direction = e.key === 'ArrowLeft' ? '-' : '+';
                    const button = els.jogPanel.querySelector(
                        `.jog-hold-btn[data-direction="${direction}"]`
                    );
                    if (button) beginJogHold(direction, button);
                }
                return;
            }
            if (e.key === 'ArrowUp' || e.key === 'ArrowDown') return;
        }

        // В паузе те же стрелки удерживают ограниченную коррекцию ленты.
        // Без этой ветки они провалились бы в переключение камер, и одна
        // и та же клавиша делала бы в JOG и в паузе разные вещи.
        if (state.pauseActive) {
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                e.preventDefault();
                if (!e.repeat && !state.nudgeHoldDirection) {
                    const direction = e.key === 'ArrowLeft' ? '-' : '+';
                    const button = els.nudgePanel && els.nudgePanel.querySelector(
                        `.nudge-btn[data-direction="${
                            direction === '-' ? 'backward' : 'forward'
                        }"]`
                    );
                    // Кнопка, заблокированная исчерпанным бюджетом или
                    // встречным движением, не должна обходиться клавишей.
                    if (button && !button.disabled) {
                        beginNudgeHold(direction, button);
                    }
                }
                return;
            }
            if (e.key === 'ArrowUp' || e.key === 'ArrowDown') return;
        }

        if (e.key === 'Tab') {
            e.preventDefault();
            if (viewModeAllowed()) toggleMode();
            return;
        }

        if (e.key === 'ArrowLeft' || e.key === 'a' || e.key === 'A') {
            navigateCamera(-1);
            return;
        }

        if (e.key === 'ArrowRight' || e.key === 'd' || e.key === 'D') {
            navigateCamera(1);
            return;
        }

        if (e.key >= '1' && e.key <= '9') {
            const idx = parseInt(e.key, 10) - 1;
            if (idx < state.cameras.length) {
                selectCamera(state.cameras[idx]);
            }
        }
    });
}

window.addEventListener('keyup', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    if (state.jogHoldDirection) {
        e.preventDefault();
        releaseJogHold(`key released: ${e.key}`);
    }
    // Отпускание клавиши обязано останавливать ленту и в паузе: иначе
    // удержание продолжилось бы до heartbeat timeout.
    if (state.nudgeHoldDirection) {
        e.preventDefault();
        releaseNudgeHold(`key released: ${e.key}`);
    }
});

// ─── Init ────────────────────────────────────────────────────

function init() {
    els.cameraContainer = document.querySelector('.camera-container');

    setupButtons();
    setupHotkeys();
    setupGallery();
    setupCameraHover();
    setupViewModeControls();
    setupJogControls();
    setupPauseControls();
    setupDistributorDiagnostics();
    setupPrestartDiagnostics();
    setupSelectedFrameAnalysis();

    fetchBoot();
    state.bootInterval = setInterval(fetchBoot, BOOT_INTERVAL);

    startUiReadyWatcher();

    setInterval(fetchCameras, 2000);
    setInterval(refreshPreviewStrip, PREVIEW_INTERVAL);
    setInterval(updateUptime, UPTIME_INTERVAL);
}

function setupForTest() {
    els.cameraContainer = document.querySelector('.camera-container');
    setupButtons();
    setupHotkeys();
    setupGallery();
    setupCameraHover();
    setupViewModeControls();
    setupJogControls();
    setupPauseControls();
    setupDistributorDiagnostics();
    setupPrestartDiagnostics();
    setupSelectedFrameAnalysis();
    state.bootDone = true;
    state.bootDoneAt = Date.now();
    state.statusReceived = true;
    state.jogReceived = true;
    state.uiReady = true;
    state.uiRevealed = true;
    state.splashActive = false;
    els.splash.classList.add('is-hidden');
    els.main.classList.remove('is-hidden');
}

if (window.__TRANSPORTER_UI_TEST__ === true) {
    window.__TRANSPORTER_UI_TEST_API__ = {
        state,
        els,
        setupForTest,
        updateLineStatus,
        updateJogState,
        updateLineCells,
        updateRecentParts,
        updateStateOverlay,
        updateMode,
        updateDistributorDiagnosticControls,
        updatePrestartDiagnostics,
        runPrestartDiagnostic,
        updateSelectedAnalysisStatus,
        showSelectedAnalysisFrame,
        returnSelectedCameraToLive,
        applyButtonsForState,
        markUiOffline,
        beginJogHold,
        releaseJogHold,
        clearJogHoldLocalState,
        beginNudgeHold,
        releaseNudgeHold,
        clearNudgeHoldLocalState,
        fetchStatus,
        fetchCameras,
        selectCamera,
        toggleMode,
        setViewMode,
        updateViewModeControls,
        openGallery,
        closeGallery,
        renderGalleryImages,
        showControlError,
        clearControlError,
        getMainBufferSource: () => mainBuffer.src,
    };
} else if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}
