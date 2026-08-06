// test_single_run_ui.mjs — рендер панелей анализа при одиночном прогоне.
//
// Тройное голосование убрано: у каждого порога ровно один замер, бейджи
// правил не показывают счётчики «· 2/3», у замеров нет data-run (клик по
// переключению кадров недоступен), модель показывает одно количество
// detections, строка «КАРТИНКА» не несёт номер прогона.
import { createSandbox, loadUI, installStubs, runInSandbox } from './harness.mjs';

const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

const body = `
    const assert = (cond, msg) => { if (!cond) throw new Error('ASSERT: ' + msg); };
    const tbody = __els['fa-new-tbody'];
    const cards = __els['frame-analysis-cards'];

    // ── Регрессия: els.frameAnalysisPanel — валидный алиас панели ──
    assert(els.frameAnalysisPanel === els.frameAnalysisPanelNew,
        'frameAnalysisPanel alias resolves to the real panel');

    // ── Бейджи: один прогон — без счётчика голосов ──
    assert(faNewVoteSummary({ decision: 'present', present_votes: 1, total_runs: 1, required_votes: 1 }).text === 'КОРПУС',
        'present badge without count');
    assert(faNewVoteSummary({ decision: 'triggered', triggered_votes: 1, total_runs: 1, required_votes: 1 }).text === 'СРАБОТАЛО',
        'triggered badge without count');
    assert(faNewVoteSummary({ decision: 'empty', empty_votes: 1, total_runs: 1, required_votes: 1 }).text === 'ПУСТО',
        'empty badge without count');
    // Мультипрогоновые данные (если INSPECTION_RUNS вернётся > 1) всё ещё показывают счётчик
    assert(faNewVoteSummary({ decision: 'normal', normal_votes: 2, total_runs: 3, required_votes: 2 }).text.includes('/3'),
        'multi-run badge keeps count');

    // ── Замеры: один слот на порог, без data-run ──
    const runCards = [[{
        role: 'INPUT_LEFT', ok: true, verdict: 'в допуске', found: [],
        metrics: [{ label: 'Смещение, px', value: '12.0', limit: '15.0', ok: true,
                    value_raw: 12.0, limit_raw: 15.0, key: 'shift' }],
    }]];
    const byRole = faCollectMetrics(runCards);
    const metric = byRole.get('INPUT_LEFT').get('shift');
    assert(metric.runs.length === 1, 'one run slot');

    const block = faBuildThresholdBlock('ВХОД', metric, 1, 1, false);
    const runChips = [];
    (function collect(node) {
        node.children.forEach(c => { if (c._attrs && c._attrs['data-run']) runChips.push(c); collect(c); });
    })(block);
    assert(runChips.length === 0, 'no data-run chips in single mode');
    const values = [];
    (function collect(node) {
        node.children.forEach(c => { if (c.classList && c.classList._set.has('fa-mv-value')) values.push(c.textContent); collect(c); });
    })(block);
    assert(values.length === 1 && values[0] === '12.0', 'single value rendered: ' + JSON.stringify(values));

    // ── Компактная панель (frame-analysis.js): одна ячейка замера ──
    const report = {
        available: true, kind: 'CYCLE', stage: 'ВХОД', part_id: 7,
        picture_run: 1, picture_reason: 'x',
        rules: [{
            name: 'part_presence', triggered: false, part_absent: false,
            vote_details: { decision: 'present', present_votes: 1, total_runs: 1, required_votes: 1 },
            run_cards: [[{ role: 'INPUT', ok: true, metrics: [{ label: 'Flatness L', value: '12',
                        limit: '30', ok: true, value_raw: 12, limit_raw: 30, key: 'flatness_left' }] }]],
        }],
        models: [],
    };
    renderNewFrameAnalysis(report);
    let measCount = 0;
    (function collect(node) {
        node.children.forEach(c => { if (c.classList && c.classList._set.has('fa-new-meas')) measCount++; collect(c); });
    })(tbody);
    assert(measCount === 1, 'exactly one measurement chip: ' + measCount);

    // ── Полная панель (rule-summary.js): решающая метрика без П1: префикса ──
    const badRunCards = [[{
        role: 'INPUT_LEFT', ok: false, verdict: 'отклонение', found: [],
        metrics: [{ label: 'Смещение, px', value: '18.0', limit: '15.0', ok: false,
                    value_raw: 18.0, limit_raw: 15.0, key: 'shift' }],
    }]];
    const rules = [{
        name: 'window_geometry', triggered: true, skipped: false, part_absent: false,
        run_cards: badRunCards,
        threshold_breaches: [{ label: 'Смещение, px', key: 'shift', role: 'INPUT_LEFT' }],
        threshold_conclusion: 'вышло за порог',
        human_cause: 'смещение',
    }];
    renderFrameAnalysisPanel(rules, 1, [], { totalRules: 1 });
    const metricChips = [];
    (function collect(node) {
        node.children.forEach(c => { if (c.classList && c.classList._set.has('fa-metric-chip')) metricChips.push(c); collect(c); });
    })(cards);
    assert(metricChips.length === 1, 'one metric chip: ' + metricChips.length);
    assert(metricChips[0].textContent === '18.0', 'no П1: prefix in single mode: ' + metricChips[0].textContent);
    assert(!metricChips[0]._attrs['data-run'], 'no run switching on single chip');

    // ── Производительность моделей: одно число detections без «П1=» ──
    const collectText = (node) => {
        let text = node.textContent || '';
        node.children.forEach(child => { text += collectText(child); });
        return text;
    };
    const perf = faBuildModelPerformance([
        { role: 'INPUT_LEFT', model: 'm', elapsed_ms: 10.0, detections_by_run: [3] },
    ]);
    const perfText = collectText(perf);
    assert(perfText.includes('Детекции: 3'), 'single detection count: ' + perfText);
    assert(!perfText.includes('П1='), 'no run-prefixed detections');

    // ── Строка «КАРТИНКА» в updateFrameAnalysisStatus не несёт номер ──
    const ls = {
        state: 'RUNNING',
        in_line: 1,
        line_parts: [{ id: 7, position: 0, category: 'GOOD' }],
        process: { phase: 'ANALYSIS_REVIEW' },
        live: { running: true, streaming: false, static: true },
        frame_analysis: {
            available: true, kind: 'CYCLE', stage: 'ВХОД', part_id: 7,
            title: 'АНАЛИЗ ТЕКУЩЕГО КАДРА',
            message: 'x',
            picture_run: 1,
            picture_reason: 'window_geometry: замер ближе всего к порогу',
            rules: [],
            models: [],
        },
        selected_analysis: { active: false, role: null },
        controls: {},
        dist1_state: 'IDLE', dist1_position: 0, dist1_max: 340,
        dist2_state: 'IDLE', dist2_position: 0, dist2_max: 340, dist2_target: '-',
        last_distributor_action: '-',
        exit_requested: false,
    };
    updateFrameAnalysisStatus(ls);
    const picEl = __els['frame-analysis-picture'];
    assert(picEl.textContent === 'КАРТИНКА: window_geometry: замер ближе всего к порогу',
        'picture line without run number: ' + picEl.textContent);

    console.log('TEST SINGLE RUN UI OK');
`;

await runInSandbox(sandbox, body);
