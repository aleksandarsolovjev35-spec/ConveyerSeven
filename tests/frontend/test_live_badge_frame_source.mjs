// Бейдж источника кадра должен переключаться после загрузки изображения,
// а не после более раннего статуса backend.
import { createSandbox, loadUI, installStubs, runInSandbox } from './harness.mjs';

const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

await runInSandbox(sandbox, `
    const assert = (value, message) => {
        if (!value) throw new Error('ASSERT: ' + message);
    };
    state.mode = 'RULES';
    state.selectedAnalysisActive = false;
    state.liveFps = 24;

    // На экране уже стоп-кадр. Новый live-источник только запрошен.
    state.displayedFrameKind = 'static';
    applyLiveBadge(false);
    assert(els.modeBadge.textContent === 'СТОП-КАДР · ПРАВИЛА',
        'initial static badge');

    showMainCameraFrame('/frame/INPUT_LEFT?live=1', 'live');
    applyLiveBadge(false);
    assert(els.modeBadge.textContent === 'СТОП-КАДР · ПРАВИЛА',
        'badge stays static until live image load');

    (els.mainCamera._listeners.load || []).forEach(fn => fn());
    assert(els.modeBadge.textContent === 'ПОТОК · 24,0 КАДР/С',
        'badge becomes live only after image load');

    // Обратный переход симметричен: до загрузки inspection-снимка поток
    // остаётся честно помечен как поток.
    showMainCameraFrame('/frame/INPUT_LEFT?v=42', 'static');
    applyLiveBadge(false);
    assert(els.modeBadge.textContent === 'ПОТОК · 24,0 КАДР/С',
        'badge stays live until static image load');

    (els.mainCamera._listeners.load || []).forEach(fn => fn());
    assert(els.modeBadge.textContent === 'СТОП-КАДР · ПРАВИЛА',
        'badge becomes static only after inspection image load');

    // Ручной «АНАЛИЗ КАДРА» также не должен заранее заменять честный
    // стоп-кадр/поток надписью «АНАЛИЗ».
    showMainCameraFrame('/frame/INPUT_LEFT?analysis=1', 'analysis');
    applyLiveBadge(false);
    assert(els.modeBadge.textContent === 'СТОП-КАДР · ПРАВИЛА',
        'badge stays static until analysis image load');
    (els.mainCamera._listeners.load || []).forEach(fn => fn());
    assert(els.modeBadge.textContent === 'АНАЛИЗ',
        'badge becomes analysis only after analysis image load');

    console.log('TEST LIVE BADGE FRAME SOURCE OK');
`);
