// test_single_run_ui.mjs — рендер компактной панели анализа при одиночном прогоне.
//
// Тройное голосование убрано: у каждого порога ровно один замер, бейджи
// правил не показывают счётчики «· 2/3», у замеров нет data-run (переключение
// кадров недоступно). Тест работает с реальным DOM из index.html: только
// компактная панель (fa-new-*), «полная» панель (frame-analysis-cards и т.п.)
// в production отсутствует.
import { createSandbox, loadUI, installStubs, runInSandbox } from './harness.mjs';

const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

const body = `
    const assert = (cond, msg) => { if (!cond) throw new Error('ASSERT: ' + msg); };
    const tbody = __els['fa-new-tbody'];
    const verdict = __els['fa-new-verdict'];
    const context = __els['fa-new-context'];
    const panel = __els['frame-analysis-panel'];

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

    // ── Компактная панель: один замер на порог, без data-run ──
    const report = {
        available: true, kind: 'CYCLE', stage: 'ВХОД', part_id: 7,
        title: 'АНАЛИЗ ТЕКУЩЕГО КАДРА',
        message: 'итог по свежему кадру',
        picture_run: 1, picture_reason: 'window_geometry: замер ближе всего к порогу',
        rules: [{
            name: 'part_presence', triggered: false, part_absent: false,
            vote_details: { decision: 'present', present_votes: 1, total_runs: 1, required_votes: 1 },
            run_cards: [[{ role: 'INPUT', ok: true, metrics: [
                { label: 'Flatness L', value: '12', limit: '30', ok: true,
                  value_raw: 12, limit_raw: 30, key: 'flatness_left' },
            ] }]],
        }],
        models: [],
    };
    renderNewFrameAnalysis(report);

    assert(!panel.classList._set.has('is-collapsed'), 'panel expanded');
    assert(verdict.textContent === 'ГОДНО', 'verdict: ' + verdict.textContent);
    assert(context.textContent.includes('ВХОД'), 'context has stage');
    assert(context.textContent.includes('КОРПУС #7'), 'context has part id');

    let measCount = 0;
    (function collect(node) {
        node.children.forEach(c => {
            if (c.classList && c.classList._set.has('fa-new-meas')) measCount++;
            collect(c);
        });
    })(tbody);
    assert(measCount === 1, 'exactly one measurement chip: ' + measCount);

    // Сработавшее правило → вердикт «БРАК» с названием правила
    const badReport = {
        available: true, kind: 'CYCLE', stage: 'ВХОД', part_id: 7,
        title: 'АНАЛИЗ ТЕКУЩЕГО КАДРА', message: 'x', picture_run: 1,
        rules: [{
            name: 'window_geometry', triggered: true, part_absent: false,
            vote_details: { decision: 'triggered', triggered_votes: 1, total_runs: 1, required_votes: 1 },
            run_cards: [[{ role: 'INPUT_LEFT', ok: false, metrics: [
                { label: 'Смещение, px', value: '18.0', limit: '15.0', ok: false,
                  value_raw: 18.0, limit_raw: 15.0, key: 'shift' },
            ] }]],
        }],
        models: [],
    };
    renderNewFrameAnalysis(badReport);
    assert(verdict.textContent.includes('БРАК'), 'bad verdict: ' + verdict.textContent);

    // ── Замеры (faNewCollectThresholds): один слот на порог ──
    const runCards = [[{
        role: 'INPUT_LEFT', ok: true, verdict: 'в допуске', found: [],
        metrics: [{ label: 'Смещение, px', value: '12.0', limit: '15.0', ok: true,
                    value_raw: 12.0, limit_raw: 15.0, key: 'shift' }],
    }]];
    const thrMap = faNewCollectThresholds(runCards);
    const thr = thrMap.get('shift');
    assert(thr, 'threshold collected');
    assert(thr.runs.length === 1, 'one run slot');

    // ── Мёртвая «полная» панель: функции не падают, но DOM отсутствует ──
    // renderFrameAnalysisPanel в production упирается в frame-analysis-cards
    // (нет в HTML) и должен безопасно выйти.
    assert(__els['frame-analysis-cards'] === null || typeof __els['frame-analysis-cards'] === 'undefined',
        'full panel cards DOM absent in production');
    let threw = false;
    try {
        renderFrameAnalysisPanel([{
            name: 'window_geometry', triggered: true, skipped: false,
            part_absent: false, run_cards: badReport.rules[0].run_cards,
            threshold_breaches: [], threshold_conclusion: 'x',
        }], 1, [], { totalRules: 1 });
    } catch (error) {
        threw = true;
    }
    assert(!threw, 'renderFrameAnalysisPanel no-crash with absent DOM');

    console.log('TEST SINGLE RUN UI OK');
`;

await runInSandbox(sandbox, body);
