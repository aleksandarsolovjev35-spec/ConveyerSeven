// test_dead_code.mjs — после чистки не осталось мёртвой обвязки прогонов.
//
// Проверяет, что функции переключения кадров прогонов удалены из UI,
// состояния viewRun/runFramesAvailable не существуют, а в исходниках нет
// следов удалённых сущностей (хоткей N, CSS run-cyclable/fa-run-status,
// set_run_rule_results).
import fs from 'node:fs';
import path from 'node:path';
import { createSandbox, loadUI, installStubs, runInSandbox } from './harness.mjs';

const ROOT = path.resolve(new URL('../..', import.meta.url).pathname);
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

// ─── 1. Функции переключения прогонов удалены из рантайма ───
const sandbox = createSandbox();
loadUI(sandbox);
installStubs(sandbox);

await runInSandbox(sandbox, `
    const assert = (cond, msg) => { if (!cond) throw new Error('ASSERT: ' + msg); };
    assert(typeof setMainCameraRun === 'undefined', 'setMainCameraRun removed');
    assert(typeof cycleMainCameraRun === 'undefined', 'cycleMainCameraRun removed');
    assert(typeof updateRunCycleAvailability === 'undefined', 'updateRunCycleAvailability removed');
    assert(typeof setupMainCameraRunCycle === 'undefined', 'setupMainCameraRunCycle removed');
    assert(typeof runBadgeSuffix === 'undefined', 'runBadgeSuffix removed');
    assert(typeof setupFrameAnalysisRunClicks === 'undefined', 'setupFrameAnalysisRunClicks removed');
    assert(state.viewRun === undefined, 'state.viewRun removed');
    assert(state.runFramesAvailable === undefined, 'state.runFramesAvailable removed');
    assert(typeof state.pictureRun === 'number', 'state.pictureRun present');
    console.log('TEST DEAD CODE (runtime) OK');
`);

// ─── 2. Исходники не содержат удалённых сущностей ───
const checks = [
    ['vision/ui/static/js/bootstrap.js', 'KeyN'],
    ['vision/ui/static/js/cameras.js', 'function setMainCameraRun'],
    ['vision/ui/static/js/cameras.js', 'run-cyclable'],
    ['vision/ui/static/js/jog.js', 'runBadgeSuffix'],
    ['vision/ui/static/js/diagnostics.js', 'setupFrameAnalysisRunClicks'],
    ['vision/ui/templates/index.html', 'N прогоны'],
    ['vision/ui/static/css/camera.css', 'run-cyclable'],
    ['vision/ui/static/css/blocks.css', 'fa-run-status'],
    ['vision/ui/server/server.py', 'def set_run_rule_results'],
    ['vision/ui/static/js/status.js', 'runFramesAvailable'],
    ['vision/ui/static/js/core.js', 'viewRun'],
];
for (const [file, needle] of checks) {
    const source = read(file);
    if (source.includes(needle)) {
        throw new Error(`DEAD CODE STILL PRESENT: ${file} contains "${needle}"`);
    }
}
console.log('TEST DEAD CODE (sources) OK');
