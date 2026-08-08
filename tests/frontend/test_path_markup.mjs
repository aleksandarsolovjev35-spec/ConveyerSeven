// test_path_markup.mjs — путь корпусов имеет реальные элементы разметки.
// Проверяет наличие самих ячеек пути и отсутствие удалённых бесполезных
// индикаторов «вход/выход» (line-gate), которые убраны из блока «Путь корпусов».
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(new URL('../..', import.meta.url).pathname);
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const html = read('vision/ui/templates/index.html');
const css = read('vision/ui/static/css/process.css');
const js = read('vision/ui/static/js/status.js');

const requiredHtml = [
    'class="line-cells production-line-cells"',
    'data-pos="0"',
    'data-pos="7"',
    'data-pos="8"',
    'class="line-cell line-cell-chute"',
    // Подписи этапов — слова без номеров (ВХОД / КОНТРОЛЬ / СОРТИРОВКА / СБРОС).
    'class="line-position-labels"',
    '>ВХОД</span>',
    '>КОНТРОЛЬ</span>',
    '>СОРТИРОВКА</span>',
    '>СБРОС</span>',
];
const requiredCss = [
    '.production-line-cells',
    '.line-token',
    '.line-cell.line-cell-chute',
];

for (const needle of requiredHtml) {
    if (!html.includes(needle)) throw new Error(`PATH MARKUP MISSING: ${needle}`);
}
for (const needle of requiredCss) {
    if (!css.includes(needle)) throw new Error(`PATH STYLE MISSING: ${needle}`);
}

// Удалённые индикаторы входа/выхода не должны присутствовать в HTML,
// CSS и JS (бесполезные элементы блока «Путь корпусов»).
for (const needle of ['line-gate-in', 'line-gate-out', '.line-gate', '_updateLineGates']) {
    if (html.includes(needle)) throw new Error(`PATH MARKUP STALE: ${needle} in html`);
    if (css.includes(needle)) throw new Error(`PATH MARKUP STALE: ${needle} in css`);
    if (js.includes(needle)) throw new Error(`PATH MARKUP STALE: ${needle} in js`);
}

console.log('TEST PATH MARKUP OK');
