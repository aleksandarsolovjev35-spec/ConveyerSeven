// thresholds.js — Line Monitor UI module
'use strict';

// Панель «Пороги правил» показывает параметры правил выбранной (главной)
// камеры с понятными русскими названиями (label приходит с сервера) и
// позволяет менять только значения. Каждое правило — собственная
// карточка-блок (стилистически как карточки распределителя), каждый
// параметр — строка «название | ползунок | число»: ползунок и числовое
// поле всегда синхронизированы. Редактирование доступно только до
// пуска (IDLE) и после полной остановки (STOPPED); во время работы
// линии панель скрыта, а backend дополнительно проверяет состояние
// при сохранении.

const THRESHOLD_EDITABLE_STATES = ['IDLE', 'STOPPED'];

let thresholdsData = null;       // последний ответ GET /api/thresholds
let thresholdsBusy = false;      // идёт загрузка порогов
let thresholdsSaveBusy = false;  // идёт сохранение порогов
let thresholdsBodyKey = null;    // роль+ревизия, для которой построены поля
let thresholdsDirty = false;     // оператор менял значения и ещё не сохранил

function thresholdsPanelVisible() {
    return (
        !state.splashActive
        && !state.offline
        && !state.serverExitRequested
        && THRESHOLD_EDITABLE_STATES.includes(state.lineState)
        && !!state.currentCamera
    );
}

function thresholdsEditableNow() {
    return (
        thresholdsPanelVisible()
        && !state.controlPending
        && !state.startPending
        && !state.jogActive
        && !state.distributorDiagnosticPending
        && !state.distributorDiagnosticBackendBusy
        && !state.selectedAnalysisPending
    );
}

function setThresholdsStatus(message, kind) {
    if (!els.thresholdsStatus) return;
    setIfChanged(els.thresholdsStatus, message || '');
    els.thresholdsStatus.classList.toggle('is-error', kind === 'error');
}

async function fetchThresholds(role) {
    if (!role || thresholdsBusy) return;
    thresholdsBusy = true;
    const data = await apiGet(`/api/thresholds?role=${encodeURIComponent(role)}`);
    thresholdsBusy = false;
    if (!data) return;
    // Гонка при быстром переключении камер: ответ прилетел после того, как
    // оператор уже выбрал другую камеру — данные старой камеры отбрасываем
    // и подгружаем актуальные.
    if (data.role !== state.currentCamera) {
        fetchThresholds(state.currentCamera);
        return;
    }
    thresholdsData = data;
    renderThresholdsPanel();
}

// Вызывается из updateLineStatus (status.js), selectCamera/fetchCameras
// (cameras.js) и при офлайне: панель следует за выбранной главной камерой.
// force=true — принудительно перечитать пороги с сервера (автоподхват
// после внешней правки thresholds.json).
function updateThresholdsPanel(force) {
    if (!thresholdsPanelVisible()) {
        if (els.thresholdsPanel) els.thresholdsPanel.classList.add('is-hidden');
        return;
    }
    if (els.thresholdsPanel) els.thresholdsPanel.classList.remove('is-hidden');

    if (!thresholdsData || thresholdsData.role !== state.currentCamera) {
        // Смена камеры отбрасывает незаконченный ввод.
        thresholdsDirty = false;
        thresholdsData = null;
        thresholdsBodyKey = null;
        setThresholdsStatus('', '');
        fetchThresholds(state.currentCamera);
        return;
    }
    // Автоподхват после внешней правки файла: не перезапрашивать, пока
    // оператор не сохранил свои незаконченные изменения.
    if (force && !thresholdsDirty && !thresholdsBusy) {
        fetchThresholds(state.currentCamera);
        return;
    }
    renderThresholdsPanel();
}

function renderThresholdsPanel() {
    if (!thresholdsData) return;

    if (els.thresholdsCameraLabel) {
        setIfChanged(els.thresholdsCameraLabel, cameraRoleLabel(state.currentCamera));
    }

    const editable = thresholdsEditableNow() && thresholdsData.editable !== false;
    if (els.thresholdsPanel) {
        els.thresholdsPanel.classList.toggle('is-locked', !editable);
    }
    if (els.thresholdsHint) {
        setIfChanged(els.thresholdsHint, editable
            ? 'Линия остановлена — значения можно менять'
            : 'Редактирование доступно до пуска и после полной остановки');
    }

    // Поля перестраиваются только при смене данных (роль/ревизия), а не на
    // каждом тике статуса — иначе незаконченный ввод оператора стирался бы
    // каждые 500 мс.
    const key = `${thresholdsData.role}|${thresholdsData.revision}`;
    if (key !== thresholdsBodyKey) {
        thresholdsBodyKey = key;
        renderThresholdsBody();
    }
    updateThresholdsActions();
}

// ─── Блоки-карточки правил ─────────────────────────────────────────
// Каждое правило — отдельная карточка (как blade-card распределителя).
// Карточки лежат в общем списке; при нехватке места прокручивается
// только список порогов, а кнопки ручного управления ниже остаются
// на своих местах.

function thresholdsSliderRange(param) {
    const lo = Number(param.min);
    const hi = Number(param.max);
    if (!Number.isFinite(lo) || !Number.isFinite(hi) || hi <= lo) return null;
    return {lo, hi};
}

function thresholdsSyncItem(item) {
    const input = item.querySelector('input.thresholds-input');
    const slider = item.querySelector('input.thresholds-slider');
    const wrap = item.querySelector('.thresholds-slider-wrap');
    if (!input || !slider || !wrap) return;
    const raw = String(input.value).trim();
    if (raw !== '') slider.value = Number(raw);
    const value = raw === '' ? Number(slider.value) : Number(raw);
    const lo = Number(slider.min) || 0;
    const hi = Number(slider.max) || 1;
    const pct = hi > lo
        ? Math.max(0, Math.min(100, (value - lo) / (hi - lo) * 100))
        : 0;
    wrap.style.setProperty('--fill', `${pct}%`);
}

function buildThresholdItem(param) {
    // Контейнер строки — div, а не label: клик по названию не должен
    // перебрасывать ползунок (label форвардит клик на range-инпут).
    const item = document.createElement('div');
    item.className = 'thresholds-item';

    const span = document.createElement('span');
    span.className = 'thresholds-item-label';
    span.textContent = param.label || param.key;
    span.title = param.key;
    item.appendChild(span);

    // Ползунок появляется, только когда backend дал осмысленный диапазон.
    // Иначе остаётся одно числовое поле — как для нестандартных порогов.
    const range = thresholdsSliderRange(param);
    if (range) {
        const wrap = document.createElement('span');
        wrap.className = 'thresholds-slider-wrap';
        const fill = document.createElement('span');
        fill.className = 'thresholds-slider-fill';
        const slider = document.createElement('input');
        slider.type = 'range';
        slider.className = 'thresholds-slider';
        slider.min = range.lo;
        slider.max = range.hi;
        slider.step = (typeof param.step === 'number' && param.step > 0)
            ? param.step
            : (range.hi - range.lo) / 100;
        slider.value = Math.min(
            range.hi,
            Math.max(range.lo, Number(param.value) || range.lo),
        );
        wrap.append(fill, slider);
        item.appendChild(wrap);
    } else {
        item.classList.add('thresholds-item-no-slider');
    }

    const input = document.createElement('input');
    input.type = 'number';
    input.className = 'thresholds-input';
    input.dataset.key = param.key;
    input.step = param.step || 'any';
    if (typeof param.min === 'number') input.min = param.min;
    if (typeof param.max === 'number') input.max = param.max;
    input.value = param.value;
    item.appendChild(input);
    return item;
}

function renderThresholdsBody() {
    const body = els.thresholdsBody;
    if (!body) return;
    body.innerHTML = '';

    const rules = (thresholdsData && thresholdsData.rules) || [];
    if (!rules.length) {
        const empty = document.createElement('div');
        empty.className = 'thresholds-empty';
        empty.textContent = 'Пороги для выбранной камеры не найдены';
        body.appendChild(empty);
        return;
    }

    const cards = document.createElement('div');
    cards.className = 'thresholds-cards';

    rules.forEach(group => {
        const card = document.createElement('section');
        card.className = 'thresholds-card';
        card.dataset.rule = group.rule || '';

        const head = document.createElement('div');
        head.className = 'thresholds-card-head';
        const title = document.createElement('span');
        title.className = 'thresholds-card-title';
        title.textContent = group.label || group.rule;
        const count = document.createElement('span');
        count.className = 'thresholds-card-count';
        count.textContent = String((group.params || []).length);
        head.append(title, count);
        card.appendChild(head);

        const rows = document.createElement('div');
        rows.className = 'thresholds-rows';
        for (const param of group.params || []) {
            const item = buildThresholdItem(param);
            thresholdsSyncItem(item);
            rows.appendChild(item);
        }
        card.appendChild(rows);
        cards.appendChild(card);
    });

    body.appendChild(cards);
}

function collectThresholdValues() {
    const values = {};
    if (els.thresholdsBody) {
        els.thresholdsBody.querySelectorAll('input.thresholds-input').forEach(input => {
            const raw = String(input.value).trim();
            if (raw === '') return;
            const number = Number(raw);
            if (Number.isNaN(number)) return;
            values[input.dataset.key] = number;
        });
    }
    return values;
}

function hasChangedThresholds() {
    if (!thresholdsData || !thresholdsData.values) return false;
    // Пустое поле — тоже незаконченное изменение: кнопка «СОХРАНИТЬ»
    // становится активной, а сохранение честно сообщает «Заполните все поля».
    if (els.thresholdsBody) {
        const hasEmpty = [...els.thresholdsBody.querySelectorAll(
            'input.thresholds-input'
        )].some(input => String(input.value).trim() === '');
        if (hasEmpty) return true;
    }
    const current = collectThresholdValues();
    return Object.entries(current).some(
        ([key, value]) => thresholdsData.values[key] !== value,
    );
}

function updateThresholdsActions() {
    if (!els.thresholdsSave || !els.thresholdsReset) return;
    // ``editable`` от API — отдельная защита: панель может быть видимой в
    // момент, когда backend уже запретил правку (например, начинается пуск).
    const editable = (
        thresholdsEditableNow()
        && !!thresholdsData
        && thresholdsData.editable !== false
    );
    // Не полагаемся только на сравнение чисел. Некоторые браузеры присылают
    // ``change`` для number-поля позже, чем оператор ожидает, а ``input`` —
    // при ручном вводе. Флаг гарантирует, что после любого редактирования
    // кнопка сохранения сразу доступна.
    const changed = thresholdsDirty || hasChangedThresholds();
    els.thresholdsReset.disabled = !editable || thresholdsSaveBusy;
    els.thresholdsSave.disabled = !editable || thresholdsSaveBusy || !changed;
}

async function saveThresholds() {
    if (!thresholdsEditableNow() || thresholdsSaveBusy || !thresholdsData) return;

    // Пустые поля не сохраняются: честно предупреждаем, а не молча
    // пропускаем параметр (проверяем до сбора значений, иначе очищенное
    // поле дало бы пустой values и молчаливый выход).
    if (els.thresholdsBody) {
        const empty = els.thresholdsBody.querySelectorAll(
            'input.thresholds-input'
        );
        const hasEmpty = [...empty].some(
            input => String(input.value).trim() === '',
        );
        if (hasEmpty) {
            setThresholdsStatus('Заполните все поля', 'error');
            return;
        }
    }

    const values = collectThresholdValues();
    if (!Object.keys(values).length) return;

    thresholdsSaveBusy = true;
    updateThresholdsActions();
    setThresholdsStatus('Сохранение...', '');
    try {
        const result = await apiPostJson('/api/thresholds', {
            role: state.currentCamera,
            values,
        }, true);
        if (!result || !result.thresholds) {
            setThresholdsStatus('Не удалось сохранить пороги', 'error');
            return;
        }
        thresholdsData = result.thresholds;
        thresholdsDirty = false;
        if (typeof result.thresholds.revision === 'number') {
            state.thresholdsRevision = result.thresholds.revision;
        }
        setThresholdsStatus('Сохранено', '');
        renderThresholdsPanel();
    } finally {
        thresholdsSaveBusy = false;
        updateThresholdsActions();
    }
}

async function resetThresholds() {
    if (!thresholdsEditableNow() || thresholdsSaveBusy) return;
    setThresholdsStatus('', '');
    // Принудительно перестраиваем поля, даже если ревизия не изменилась:
    // нужно сбросить незаконченный ввод оператора.
    thresholdsDirty = false;
    thresholdsBodyKey = null;
    thresholdsData = null;
    await fetchThresholds(state.currentCamera);
}

function setupThresholdsControls() {
    if (els.thresholdsSave) {
        els.thresholdsSave.addEventListener('click', saveThresholds);
    }
    if (els.thresholdsReset) {
        els.thresholdsReset.addEventListener('click', resetThresholds);
    }
    if (els.thresholdsBody) {
        const markThresholdsChanged = event => {
            // Делегирование сохраняет обработчик и после перестроения полей.
            const target = event.target;
            if (!target.matches(
                'input.thresholds-input, input.thresholds-slider'
            )) return;
            const item = target.closest('.thresholds-item');
            if (item) {
                const input = item.querySelector('input.thresholds-input');
                const slider = item.querySelector('input.thresholds-slider');
                // Ползунок — источник значения: число повторяет его шаг.
                // Ручной ввод числа, наоборот, двигает ползунок и заливку.
                if (target === slider && input) input.value = slider.value;
                thresholdsSyncItem(item);
            }
            thresholdsDirty = true;
            // Если оператор вернул значения как было (0.3 -> 0.5 -> 0.3),
            // изменений больше нет — снимаем блокировку автоподхвата.
            if (!hasChangedThresholds()) thresholdsDirty = false;
            updateThresholdsActions();
        };
        // ``input`` покрывает набор с клавиатуры и движение ползунка,
        // ``change`` — автозаполнение number-поля в браузерах, где input
        // приходит поздно.
        els.thresholdsBody.addEventListener('input', markThresholdsChanged);
        els.thresholdsBody.addEventListener('change', markThresholdsChanged);
    }
    updateThresholdsPanel();
}
