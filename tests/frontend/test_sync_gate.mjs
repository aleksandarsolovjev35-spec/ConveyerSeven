// test_sync_gate.mjs — синхронизация «сначала монитор, потом UI».
//
// Проверяет, что при статичной публикации анализа (frame_version изменился,
// лента стоит) фронтенд держит цвет корпуса на линии, появление новых
// корпусов, карточки правил и превью, пока кадр с обрисовкой не показан
// на главной камере, и применяет их одним махом в flushPendingAnalysis().
import { createSandbox, loadUI, installStubs, runInSandbox } from './harness.mjs';

const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

// Счётчик отрисовок панели анализа (подменяем реальную функцию)
sandbox.updateNewFrameAnalysisStatus = () => {
    sandbox._panelRenders = (sandbox._panelRenders || 0) + 1;
};

const body = `
    const assert = (cond, msg) => { if (!cond) throw new Error('ASSERT: ' + msg); };
    const lineCells = __els['line-cells'];

    const ls = (parts, extra = {}) => ({
        state: 'RUNNING',
        in_line: parts.length,
        line_parts: parts,
        process: { phase: 'ANALYSIS_REVIEW', label: 'Просмотр', positions: [0, 4] },
        live: { running: true, streaming: false, static: true, stage: 'REVIEW' },
        controls: {},
        dist1_state: 'IDLE', dist1_position: 0, dist1_max: 340,
        dist2_state: 'IDLE', dist2_position: 0, dist2_max: 340, dist2_target: '-',
        last_distributor_action: '-',
        frame_analysis: {
            available: true, kind: 'CYCLE', active: true, stage: 'ВХОД',
            part_id: 6, message: 'x',
            rules: [{ name: 'part_presence', triggered: false }],
            updated_at: Date.now(),
        },
        selected_analysis: { active: false, role: null },
        jog: null,
        exit_requested: false,
        ...extra,
    });

    // Базовое состояние
    state.splashActive = false;
    state.bootDone = true;
    state.bootDoneAt = Date.now();
    state.mainCamMode = 'pull';
    state.currentCamera = 'INPUT_LEFT';
    state.mode = 'RULES';
    state.lineState = 'RUNNING';
    state.liveStreaming = false;
    state.liveStatic = true;
    state.lastSeenVersion = 10;
    state.currentVersion = 10;
    state.pendingAnalysisVersion = null;
    state.pendingFlushTimer = null;
    state.lastLineStatus = null;
    state.lastStatusAt = Date.now();
    state.statusReceived = true;
    state.offline = false;

    // ── 1. Применённый снимок: #5 на позиции 4, UNKNOWN ──
    updateLineCells(ls([{ id: 5, position: 4, category: 'UNKNOWN' }]).line_parts, {});

    // ── 2. Публикация анализа: #5 -> BAD, новый #6 на входе ──
    const publishLs = ls([
        { id: 5, position: 4, category: 'BAD' },
        { id: 6, position: 0, category: 'UNKNOWN' },
    ]);
    state.pendingAnalysisVersion = 11;   // эмулируем fetchStatus: гейт включён
    state.lastLineStatus = publishLs;
    updateLineStatus(publishLs);

    const token5 = lineCells.children.find(c => c.dataset.partId === '5');
    assert(token5, 'token #5 exists');
    assert(!token5.classList._set.has('cell-bad'), '#5 must keep UNKNOWN while pending');
    assert(!lineCells.children.some(c => c.dataset.partId === '6'), '#6 must not appear while pending');
    assert((globalThis._panelRenders || 0) === 0, 'analysis panel must not render while pending');

    // Превью в pending не трогает DOM (ранний выход из refreshPreviewStrip)
    const domCallsBefore = __els['preview-strip']._queryCalls;
    refreshPreviewStrip();
    assert(__els['preview-strip']._queryCalls === domCallsBefore,
        'strip must stay frozen while pending');

    // ── 3. Кадр с обрисовкой пришёл: flush ──
    state.pendingFlushTimer = 0;
    flushPendingAnalysis();
    assert(state.pendingAnalysisVersion === null, 'pending cleared after flush');
    assert(token5.classList._set.has('cell-bad'), '#5 flipped to BAD after flush');
    assert(lineCells.children.some(c => c.dataset.partId === '6'), '#6 appeared after flush');
    assert((globalThis._panelRenders || 0) >= 1, 'analysis panel rendered after flush');
    assert(__els['preview-strip']._queryCalls > domCallsBefore,
        'strip refreshed after flush');

    // ── 4. Fallback-таймер снимает гейт, если кадр не пришёл ──
    state.pendingAnalysisVersion = 12;
    state.pendingFlushTimer = 0;
    armPendingFlushFallback();
    assert(state.pendingAnalysisVersion === 12, 'pending held until frame/timer');
    __flushTimers();
    assert(state.pendingAnalysisVersion === null, 'fallback timer flushes pending');

    // ── 5. Гейт не блокирует движение линии (позиции меняются) ──
    state.pendingAnalysisVersion = null;
    updateLineCells(ls([{ id: 5, position: 4, category: 'BAD' }]).line_parts, {});
    state.pendingAnalysisVersion = 13;
    updateLineCells(ls([{ id: 5, position: 5, category: 'BAD' }]).line_parts, {});
    const moved = lineCells.children.find(c => c.dataset.partId === '5');
    assert(moved, '#5 still rendered while pending');
    assert(moved.style.left !== undefined, 'position applied while pending');

    // ── 6. fetchStatus: статичная публикация включает гейт ──
    const payload = {
        splash_active: false,
        frame_version: 21,
        frame_versions: { INPUT_LEFT: 3, SPIDER_LEFT: 3 },
        frame_runs: 1,
        mode: 'RULES',
        thresholds_revision: 0,
        active_camera: 'INPUT_LEFT',
        line_status: ls([
            { id: 5, position: 4, category: 'BAD' },
            { id: 6, position: 0, category: 'UNKNOWN' },
        ]),
        recent_parts: [],
    };
    setFetchPayload(payload);
    state.lastSeenVersion = 20;
    state.currentVersion = 20;
    state.pendingAnalysisVersion = null;
    await fetchStatus();
    assert(state.pendingAnalysisVersion === 21,
        'fetchStatus arms the gate on static publish');
    assert(state.lastLineStatus === payload.line_status, 'lastLineStatus stored');

    // В движении (live.streaming=true) гейт НЕ включается
    payload.frame_version = 22;
    payload.line_status = ls([{ id: 5, position: 4, category: 'BAD' }],
        { live: { running: true, streaming: true, static: false, stage: 'MOTION' } });
    state.lastSeenVersion = 21;
    state.currentVersion = 21;
    state.pendingAnalysisVersion = null;
    await fetchStatus();
    assert(state.pendingAnalysisVersion === null, 'no gate while streaming');

    console.log('TEST SYNC GATE OK');
`;

// fetchImpl: по умолчанию отдаём пустой ответ; тест переопределяет payload
let fetchPayload = null;
sandbox.fetch = async (path) => {
    if (fetchPayload !== null) {
        return {
            ok: true,
            headers: { get: () => 'application/json' },
            json: async () => fetchPayload,
            text: async () => '',
        };
    }
    return { ok: false, headers: { get: () => null }, json: async () => ({}) };
};
sandbox.setFetchPayload = (payload) => { fetchPayload = payload; };

await runInSandbox(sandbox, body);
