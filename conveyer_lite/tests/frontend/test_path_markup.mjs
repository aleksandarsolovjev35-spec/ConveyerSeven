// test_path_markup.mjs — путь корпусов имеет реальные элементы для route-индикации.
// Одних querySelector-заглушек в тесте логики недостаточно: без этих узлов
// frontend обновляет состояние ворот в пустоту.
import fs from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(new URL('../..', import.meta.url).pathname);
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');
const html = read('vision/ui/templates/index.html');
const css = read('vision/ui/static/css/process.css');

const requiredHtml = [
    'class="line-gate line-gate-in"',
    'class="line-gate line-gate-out"',
    'aria-live="polite"',
];
const requiredCss = [
    '.line-gate.gate-active',
    '.line-gate.gate-rejecting',
    '.line-gate.gate-cleanup',
];

for (const needle of requiredHtml) {
    if (!html.includes(needle)) throw new Error(`PATH MARKUP MISSING: ${needle}`);
}
for (const needle of requiredCss) {
    if (!css.includes(needle)) throw new Error(`PATH STYLE MISSING: ${needle}`);
}

console.log('TEST PATH MARKUP OK');
