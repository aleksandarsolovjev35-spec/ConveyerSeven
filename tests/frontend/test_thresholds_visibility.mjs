// test_thresholds_visibility.mjs — пороги доступны только в JOG,
// но не во время анализа уже зафиксированного кадра.
import {
    createSandbox,
    installStubs,
    loadThresholds,
    loadUI,
    runInSandbox,
} from './harness.mjs';

const sandbox = createSandbox();
loadUI(sandbox);
loadThresholds(sandbox);
installStubs(sandbox);

await runInSandbox(sandbox, `
    const assert = (condition, message) => {
        if (!condition) throw new Error('ASSERT: ' + message);
    };

    Object.assign(state, {
        splashActive: false,
        offline: false,
        serverExitRequested: false,
        lineState: 'IDLE',
        currentCamera: 'INPUT_LEFT',
        jogActive: true,
        jogTogglePending: false,
        selectedAnalysisActive: false,
        selectedAnalysisPending: false,
    });

    assert(thresholdsPanelVisible(), 'thresholds visible in JOG before analysis');
    assert(thresholdsEditableNow(), 'thresholds editable in JOG before analysis');

    // Нажатие «АНАЛИЗ КАДРА»: скрываем сразу, ещё до ответа backend.
    state.selectedAnalysisPending = true;
    assert(!thresholdsPanelVisible(), 'thresholds hidden while analysis is pending');
    assert(!thresholdsEditableNow(), 'thresholds not editable while analysis is pending');

    // Стоп-кадр уже проанализирован: любые правки изменили бы пороги
    // задним числом по отношению к показанному результату.
    state.selectedAnalysisPending = false;
    state.selectedAnalysisActive = true;
    assert(!thresholdsPanelVisible(), 'thresholds hidden during active analysis');
    assert(!thresholdsEditableNow(), 'thresholds not editable during active analysis');

    // После возврата к потоку редактор снова доступен в JOG.
    state.selectedAnalysisActive = false;
    assert(thresholdsPanelVisible(), 'thresholds restored after returning to live stream');

    console.log('TEST THRESHOLDS VISIBILITY OK');
`);
