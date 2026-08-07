// harness.mjs — заглушенный DOM + загрузка UI-модулей в VM для Node-тестов.
//
// Загружает те же модули, что и index.html (в том же порядке), но без
// реального браузера: document/window/Image заменены заглушками. Функции
// из модулей, которые тест не загружает (controls/history/thresholds/boot),
// подставляются no-op-заглушками, чтобы реальные функции status/cameras/
// diagnostics/jog могли выполняться.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const ROOT = path.resolve(new URL('../..', import.meta.url).pathname);

// ─── Фейковый DOM-элемент ────────────────────────────────────

export function makeEl(tag = 'div') {
    const el = {
        tag,
        textContent: '',
        innerHTML: '',
        dataset: {},
        style: {},
        children: [],
        childNodes: [],
        parentNode: null,
        _attrs: {},
        _className: '',
        _listeners: {},
        offsetWidth: 100,
        offsetHeight: 20,
        scrollHeight: 0,
        clientHeight: 0,
        scrollTop: 0,
        setAttribute(key, value) { this._attrs[key] = String(value); },
        removeAttribute(key) { delete this._attrs[key]; },
        addEventListener(name, fn) { (this._listeners[name] ||= []).push(fn); },
        removeEventListener() {},
        classList: {
            _set: new Set(),
            add(...names) { names.forEach(n => this._set.add(n)); },
            remove(...names) { names.forEach(n => this._set.delete(n)); },
            toggle(name, force) {
                const on = force === undefined ? !this._set.has(name) : !!force;
                if (on) this._set.add(name); else this._set.delete(name);
                return on;
            },
            contains(name) { return this._set.has(name); },
        },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
        insertBefore(child) { child.parentNode = this; this.children.push(child); return child; },
        removeChild(child) {
            const index = this.children.indexOf(child);
            if (index >= 0) this.children.splice(index, 1);
            return child;
        },
        replaceChildren() { this.children = []; },
        getBoundingClientRect() { return { left: 0, top: 0, width: 200, height: 40 }; },
    };
    Object.defineProperty(el, 'className', {
        get() { return el._className; },
        set(value) {
            el._className = String(value);
            el.classList._set = new Set(el._className.split(/\s+/).filter(Boolean));
        },
    });
    return el;
}

// ─── Идентификаторы DOM, которые реально использует UI ────────
// Список синхронизирован с vision/ui/templates/index.html: только те id,
// которые есть в HTML. Id «полной» панели анализа (frame-analysis-cards,
// frame-analysis-verdict и т.п.) в HTML отсутствуют — в production они
// недостижимы, и в харнессе их быть не должно.

const UI_IDS = [
    'splash', 'splash-message', 'splash-progress-fill', 'splash-error',
    'splash-error-message', 'splash-exit', 'main', 'state-section',
    'state-indicator', 'state-label', 'metric-step', 'metric-uptime',
    'preview-strip', 'camera-label', 'mode-badge', 'main-camera',
    'camera-overlay', 'view-mode-toggle', 'analyze-selected-frame',
    'dist1-state', 'dist1-pos', 'dist1-max', 'dist1-blade', 'dist1-target',
    'dist1-card',
    'dist2-state', 'dist2-pos', 'dist2-max', 'dist2-blade', 'dist2-target',
    'dist2-card',
    'dist-action', 'dist-route', 'distributor-diagnostics', 'control-error',
    'stats-summary', 'stats-body', 'stats-service', 'history-cards',
    'stats-panel', 'stat-total', 'stat-good', 'stat-bad', 'stat-cleanup',
    'stat-inline', 'stat-empty', 'line-cells', 'process-phase-label',
    'line-state-legend', 'defects-section', 'defects-title', 'defects-list', 'jog-panel',
    'jog-last-action', 'jog-hw-serial', 'jog-hw-cameras', 'jog-hw-conveyor',
    'jog-hw-dist1', 'jog-hw-dist2', 'frame-analysis-panel',
    'archive-settings-open', 'archive-settings-group', 'archive-settings-modal', 'archive-settings-close',
    'archive-settings-cancel', 'archive-pick-folder', 'archive-settings-save',
    'archive-root-path', 'archive-jpeg-quality', 'archive-zip-compression',
    'archive-zip-level', 'archive-enabled', 'archive-compress-on-shutdown', 'archive-delete-original',
    'archive-settings-validation', 'archive-settings-status', 'archive-batch-id',
    'archive-batch-good', 'archive-batch-bad', 'archive-batch-cleanup',
    'btn-start',
    'btn-pause', 'btn-resume', 'btn-stop', 'btn-exit', 'thresholds-panel',
    'thresholds-camera-label', 'thresholds-hint', 'thresholds-body',
    'thresholds-status', 'thresholds-save', 'thresholds-reset',
    'gallery-modal', 'gallery-grid', 'gallery-part-id', 'gallery-category',
    'gallery-decision', 'gallery-time', 'gallery-batch',
    'gallery-defects-list', 'gallery-close', 'gallery-mode-debug',
    'gallery-mode-raw',
    // Компактная панель анализа кадра (frame-analysis.js)
    'fa-new-body', 'fa-new-verdict', 'fa-new-context', 'fa-new-tbody',
    // Статистика корпусов в панели анализа кадра
    'fa-new-stats', 'fa-new-stat-total', 'fa-new-stat-good',
    'fa-new-stat-bad', 'fa-new-stat-cleanup',
];

// ─── Песочница ────────────────────────────────────────────────

export function createSandbox({ fetchImpl } = {}) {
    const els = {};
    for (const id of UI_IDS) els[id] = makeEl();

    // line-cells: 9 ячеек — 8 позиций ленты (+0 … +7) и зона сброса +8
    // (лоток между +7 и +8 по фактической логике сортировки).
    const lineCells = makeEl('div');
    lineCells.querySelector = () => null;
    lineCells.querySelectorAll = (selector) => {
        if (selector === '.line-cell[data-pos]') {
            return Array.from({ length: 9 }, (_, index) => {
                const cell = makeEl('div');
                cell.dataset.pos = String(index);
                cell.getBoundingClientRect = () => ({
                    left: index * 50, top: 0, width: 44, height: 36,
                });
                return cell;
            });
        }
        return [];
    };
    lineCells.style.setProperty = () => {};
    els['line-cells'] = lineCells;

    // preview-strip: считаем обращения к DOM, чтобы проверять гейт превью
    els['preview-strip']._queryCalls = 0;
    els['preview-strip'].querySelectorAll = (selector) => {
        els['preview-strip']._queryCalls += 1;
        if (selector === '.preview-cam img') return [];
        if (selector === '.preview-cam') return [];
        return [];
    };

    class FakeImage {
        constructor() {
            this.src = '';
            this._listeners = {};
        }
        addEventListener(name, fn) { (this._listeners[name] ||= []).push(fn); }
        dispatch(name) { (this._listeners[name] || []).forEach(fn => fn()); }
    }

    const timers = [];
    const sandbox = {
        console,
        document: {
            readyState: 'loading',   // bootstrap.js не вызывает init()
            createElement: () => makeEl(),
            createTextNode: (text) => {
                const node = makeEl('text');
                node.textContent = String(text);
                return node;
            },
            getElementById: (id) => els[id] || null,
            addEventListener() {},
            querySelector: () => null,
            fullscreenElement: null,
            documentElement: { requestFullscreen() { return Promise.resolve(); } },
            exitFullscreen() { return Promise.resolve(); },
        },
        window: {
            addEventListener() {},
            removeEventListener() {},
            requestAnimationFrame: (fn) => fn(),
            __TRANSPORTER_UI_TEST__: false,
            innerWidth: 1280,
            innerHeight: 720,
        },
        Image: FakeImage,
        requestAnimationFrame: (fn) => fn(),
        setTimeout: (fn) => { timers.push(fn); return timers.length; },
        clearTimeout: () => {},
        setInterval: () => 0,
        clearInterval: () => {},
        fetch: async (path, options) => {
            if (fetchImpl) return fetchImpl(path, options);
            return {
                ok: false,
                headers: { get: () => null },
                json: async () => ({}),
                text: async () => '',
            };
        },
        Date, Math, JSON, Number, String, Boolean, Object, Array, Map, Set,
        isNaN, parseFloat, parseInt, encodeURIComponent, decodeURIComponent,
        // Доступ к песочнице из тестового тела
        __els: els,
        __timers: timers,
        __flushTimers() { timers.splice(0).forEach(fn => fn()); },
    };
    sandbox.window.document = sandbox.document;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    return sandbox;
}

// ─── Загрузка UI-модулей ──────────────────────────────────────

const UI_SCRIPTS = [
    'core.js',
    'status.js',
    'cameras.js',
    'frame-analysis.js',
    'diagnostics.js',
    'jog.js',
    'archive.js',
    'bootstrap.js',
];

export function loadUI(sandbox) {
    for (const file of UI_SCRIPTS) {
        const code = fs.readFileSync(path.join(ROOT, 'vision/ui/static/js', file), 'utf8');
        vm.runInContext(code, sandbox, { filename: file });
    }
}

// `thresholds.js` намеренно не входит в обычный набор: большинству
// фронтенд-тестов не нужен его DOM. Подключаем его адресно там, где
// проверяется сама панель порогов.
export function loadThresholds(sandbox) {
    const file = 'thresholds.js';
    const code = fs.readFileSync(path.join(ROOT, 'vision/ui/static/js', file), 'utf8');
    vm.runInContext(code, sandbox, { filename: file });
}

// ─── Заглушки функций из незагруженных модулей ────────────────

const EXTERNAL_STUBS = [
    'updateRecentParts',      // history.js
    'updateStateOverlay',     // controls.js
    'applyButtonsForState',   // controls.js
    'updateThresholdsPanel',  // thresholds.js
    'checkUiReady',           // boot.js
    'updateSplashWaitingMessage', // boot.js
    'revealUi',               // boot.js
    'fetchBoot',              // boot.js
    'startUiReadyWatcher',    // boot.js
    'setupGallery',           // history.js
    'openGallery',            // history.js
    'closeGallery',           // history.js
    'renderGalleryImages',    // history.js
    'showControlError',       // core.js (уже есть — не перезаписываем)
    'clearControlError',      // core.js (уже есть — не перезаписываем)
];

export function installStubs(sandbox) {
    for (const name of EXTERNAL_STUBS) {
        if (typeof sandbox[name] === 'undefined') {
            sandbox[name] = () => undefined;
        }
    }
}

// ─── Выполнение тестового тела в контексте ────────────────────

export async function runInSandbox(sandbox, body, filename = 'test-body.js') {
    return vm.runInContext(`(async () => {\n'use strict';\n${body}\n})();`, sandbox, { filename });
}
