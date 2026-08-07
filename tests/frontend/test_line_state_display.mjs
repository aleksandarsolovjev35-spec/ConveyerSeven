// test_line_state_display.mjs — блок «СОСТОЯНИЕ ЛЕНТЫ» показывает
// операторское название текущего состояния, а не внутреннюю фазу шага.
import fs from 'node:fs';
import path from 'node:path';
import { createSandbox, installStubs, loadUI, runInSandbox } from './harness.mjs';

const ROOT = path.resolve(new URL('../..', import.meta.url).pathname);
const html = fs.readFileSync(path.join(ROOT, 'vision/ui/templates/index.html'), 'utf8');
if (!html.includes('class="process-phase-title">СОСТОЯНИЕ ЛЕНТЫ</div>')
    || !html.includes('id="process-phase-label"')
    || !html.includes('data-line-state="IDLE">ГОТОВА К ПУСКУ</div>')) {
    throw new Error('LINE STATE BLOCK MISSING OR HAS A NON-STATE DEFAULT');
}

const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

await runInSandbox(sandbox, `
    const assert = (condition, message) => {
        if (!condition) throw new Error('ASSERT: ' + message);
    };
    const display = __els['process-phase-label'];
    const states = {
        IDLE: 'ГОТОВА К ПУСКУ',
        RUNNING: 'РАБОТАЕТ',
        PAUSED: 'ПАУЗА · КОРРЕКЦИЯ ЛЕНТЫ',
        STOPPING: 'ОСТАНОВКА ЛИНИИ',
        STOPPED: 'ОСТАНОВЛЕНА',
        FAULT: 'АВАРИЯ',
        OFFLINE: 'НЕТ СВЯЗИ',
    };

    for (const [stateName, label] of Object.entries(states)) {
        updateProcessPhaseLabel(stateName);
        assert(display.textContent === label,
            stateName + ': label ' + display.textContent);
        assert(display.dataset.lineState === stateName,
            stateName + ': state marker');
    }

    // Детальная фаза шага: «РАБОТАЕТ · ЛЕНТА ДВИЖЕТСЯ» и т.п.
    updateProcessPhaseLabel('RUNNING', 'CONVEYOR_MOVING');
    assert(display.textContent === 'РАБОТАЕТ · ЛЕНТА ДВИЖЕТСЯ',
        'RUNNING + CONVEYOR_MOVING: ' + display.textContent);
    updateProcessPhaseLabel('RUNNING', 'CAMERA_CAPTURE');
    assert(display.textContent === 'РАБОТАЕТ · СЪЁМКА КАМЕР',
        'RUNNING + CAMERA_CAPTURE: ' + display.textContent);
    updateProcessPhaseLabel('RUNNING', 'ANALYSIS_REVIEW');
    assert(display.textContent === 'РАБОТАЕТ · ПРОСМОТР АНАЛИЗА',
        'RUNNING + ANALYSIS_REVIEW: ' + display.textContent);
    // В останове детальную фазу не добавляем.
    updateProcessPhaseLabel('STOPPED', 'CONVEYOR_MOVING');
    assert(display.textContent === 'ОСТАНОВЛЕНА',
        'STOPPED ignores phase: ' + display.textContent);

    console.log('TEST LINE STATE DISPLAY OK');
`);
