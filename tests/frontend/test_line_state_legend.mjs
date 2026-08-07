// test_line_state_legend.mjs — в HMI перечислены все состояния ленты,
// а активным остаётся ровно состояние из очередного статуса.
import fs from 'node:fs';
import path from 'node:path';
import { createSandbox, installStubs, loadUI, runInSandbox } from './harness.mjs';

const ROOT = path.resolve(new URL('../..', import.meta.url).pathname);
const html = fs.readFileSync(path.join(ROOT, 'vision/ui/templates/index.html'), 'utf8');
const states = [
    ['IDLE', 'ГОТОВА К ПУСКУ'],
    ['RUNNING', 'РАБОТАЕТ'],
    ['PAUSED', 'ПАУЗА'],
    ['STOPPING', 'ОСТАНОВКА'],
    ['STOPPED', 'ОСТАНОВЛЕНА'],
    ['FAULT', 'АВАРИЯ'],
    ['OFFLINE', 'НЕТ СВЯЗИ'],
];

for (const [state, label] of states) {
    const item = `data-line-state="${state}"`;
    if (!html.includes(item) || !html.includes(label)) {
        throw new Error(`LINE STATE LEGEND MISSING: ${state} / ${label}`);
    }
}

const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

await runInSandbox(sandbox, `
    const assert = (condition, message) => {
        if (!condition) throw new Error('ASSERT: ' + message);
    };
    const legend = __els['line-state-legend'];
    const states = ['IDLE', 'RUNNING', 'PAUSED', 'STOPPING', 'STOPPED', 'FAULT', 'OFFLINE'];
    const items = states.map(stateName => {
        const item = document.createElement('span');
        item.dataset.lineState = stateName;
        return item;
    });
    legend.querySelectorAll = selector => selector === '[data-line-state]' ? items : [];

    for (const stateName of states) {
        updateLineStateLegend(stateName);
        const active = items.filter(item => item.classList.contains('is-active'));
        assert(active.length === 1, stateName + ': exactly one active item');
        assert(active[0].dataset.lineState === stateName, stateName + ': active item');
        assert(active[0]._attrs['aria-current'] === 'true', stateName + ': aria-current');
    }

    console.log('TEST LINE STATE LEGEND OK');
`);
