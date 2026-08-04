// thresholds.js — Line Monitor UI module
'use strict';

// «Пороги правил»: вкладки правил, фиксированная высота панели (правая
// колонка не скроллится). Ползунок (дорожка+бегунок) — только при
// переполнении строк; верх = начало, низ = конец. Правка: IDLE/STOPPED.

const THRESHOLD_EDITABLE_STATES = ['IDLE', 'STOPPED'];

let thresholdsData = null;       // последний ответ GET /api/thresholds
let thresholdsBusy = false;      // идёт загрузка порогов
let thresholdsSaveBusy = false;  // идёт сохранение порогов
let thresholdsBodyKey = null;    // роль+ревизия, для которой построены поля
let thresholdsDirty = false;     // оператор менял значения и ещё не сохранил
let thresholdsCardIndex = 0;     // индекс активной карточки-категории правил

function thresholdsPanelVisible() {
    return (
        !state.splashActive
        && !state.offline
        && !state.serverExitRequested
        // Во время анализа кадра (выбранный кадр) блок порогов правил не
        // показывается — оператору нужен только блок анализа.
        && !state.selectedAnalysisActive
        && THRESHOLD_EDITABLE_STATES.includes(state.lineState)
        && !!state.currentCamera
    );
}

function thresholdsEditableNow() {
    return (
        thresholdsPanelVisible()
        && !state.controlPending
        && !state.startPending
        // JOG-режим (jog.active) включается автоматически в IDLE/STOPPED,
        // поэтому блокировать редактирование на весь ручной ход нельзя:
        // кнопка «СОХРАНИТЬ» была бы недоступна всегда. Блокируем только
        // на время реального движения ленты (jog.busy).
        && !state.jogBusy
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
    // Панель могла быть скрыта (display:none), пока список в неё не
    // влезал/переставал влезать: ползунок прокрутки пересчитывается
    // при каждом показе, а не только при перестроении полей.
    thresholdsSyncScroll();

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

// ─── Карточки правил + вкладки + ползунок при переполнении ────────

function buildThresholdItem(param) {
    // Контейнер строки — div: клик по названию ничего не переключает.
    const item = document.createElement('div');
    item.className = 'thresholds-item';

    const span = document.createElement('span');
    span.className = 'thresholds-item-label';
    span.textContent = param.label || param.key;
    span.title = param.key;
    item.appendChild(span);

    // Значение задаётся только числовым полем.
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

    // Новая порция данных — снова первая вкладка.
    thresholdsCardIndex = 0;

    const scroll = document.createElement('div');
    scroll.className = 'thresholds-scroll';

    // Вкладки правил (равные доли ширины).
    const tabs = document.createElement('div');
    tabs.className = 'thresholds-tabs';
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Правила камеры');
    rules.forEach((group, index) => {
        const tab = document.createElement('button');
        tab.type = 'button';
        tab.className = 'thresholds-tab';
        tab.dataset.rule = group.rule || '';
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-selected', 'false');
        tab.title = group.label || group.rule;
        const tabLabel = document.createElement('span');
        tabLabel.className = 'thresholds-tab-label';
        tabLabel.textContent = group.label || group.rule;
        tab.appendChild(tabLabel);
        tab.addEventListener('click', () => {
            if (thresholdsCardIndex === index) {
                // Повторный клик по активной вкладке: пересчитать ползунок
                // (высота панели фиксирована, сворачивать нечего).
                thresholdsSyncScroll();
                return;
            }
            thresholdsCardIndex = index;
            updateCardVisibility();
        });
        tabs.appendChild(tab);
    });

    // Hover: раскрыть название (scrollWidth → flex-basis px).
    const THRESHOLDS_TAB_PAD = 16;
    const hoverCapable = !window.matchMedia
        || window.matchMedia('(hover: hover)').matches;
    tabs.addEventListener('pointerover', event => {
        if (!hoverCapable) return;
        const tab = event.target.closest('.thresholds-tab');
        if (!tab) return;
        const label = tab.querySelector('.thresholds-tab-label');
        const textWidth = label ? label.scrollWidth : 0;
        const needed = textWidth + THRESHOLDS_TAB_PAD;
        tab.classList.add('is-title-expanded');
        tab.style.flexBasis = needed + 'px';
    });
    tabs.addEventListener('pointerout', event => {
        if (!hoverCapable) return;
        const tab = event.target.closest('.thresholds-tab');
        if (!tab) return;
        const next = event.relatedTarget;
        if (next && tab.contains(next)) return;
        tab.classList.remove('is-title-expanded');
        tab.style.flexBasis = '';
    });
    scroll.appendChild(tabs);

    const cards = document.createElement('div');
    cards.className = 'thresholds-cards';

    rules.forEach((group, index) => {
        const card = document.createElement('section');
        card.className = 'thresholds-card';
        card.dataset.rule = group.rule || '';
        card.dataset.index = String(index);

        // Тело карточки: строки параметров + собственный вертикальный
        // ползунок прокрутки (виден только при переполнении строк).
        const cardBody = document.createElement('div');
        cardBody.className = 'thresholds-card-body';

        const rows = document.createElement('div');
        rows.className = 'thresholds-rows';
        for (const param of group.params || []) {
            rows.appendChild(buildThresholdItem(param));
        }
        cardBody.appendChild(rows);

        // Кастомный скролл в стиле приложения: дорожка + квадратик.
        // Квадратик своим положением показывает, где находится оператор:
        // сверху — начало списка, снизу — конец.
        const track = document.createElement('div');
        track.className = 'thresholds-scroll-track thresholds-card-scroll-track';
        track.setAttribute('aria-label', 'Прокрутка карточки правил');
        track.title = 'Прокрутка карточки правил';
        track.tabIndex = 0;
        const thumb = document.createElement('div');
        thumb.className = 'thresholds-scroll-thumb';
        track.appendChild(thumb);
        cardBody.appendChild(track);

        card.appendChild(cardBody);
        cards.appendChild(card);
    });

    scroll.append(cards);
    body.appendChild(scroll);

    // Активная карточка + вкладки (is-expanded — маркер, высота фиксирована).
    const syncTabHints = () => {
        [...tabs.querySelectorAll('.thresholds-tab')].forEach((tab, index) => {
            const label = rules[index].label || rules[index].rule;
            tab.title = label;
        });
    };

    const updateCardVisibility = () => {
        const total = rules.length;
        thresholdsCardIndex = Math.max(0, Math.min(total - 1, thresholdsCardIndex));
        [...cards.querySelectorAll('.thresholds-card')].forEach((card, index) => {
            const isActive = index === thresholdsCardIndex;
            card.classList.toggle('is-expanded', isActive);
            card.classList.toggle('is-active', isActive);
        });
        [...tabs.querySelectorAll('.thresholds-tab')].forEach((tab, index) => {
            const isActive = index === thresholdsCardIndex;
            tab.classList.toggle('is-active', isActive);
            tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
        });
        syncTabHints();
        thresholdsSyncScroll();
        thresholdsScrollActiveTabIntoView();
        // После смены карточки layout может догнать flex-высоту —
        // ползунок пересчитываем ещё раз на следующем кадре.
        window.requestAnimationFrame(() => {
            thresholdsSyncScroll();
            thresholdsScrollActiveTabIntoView();
        });
    };

    // Ползунок: drag/клик по дорожке (верх = начало, низ = конец).
    let dragState = null;

    function clamp(value, min, max) {
        return Math.max(min, Math.min(max, value));
    }

    function onDocMouseMove(e) {
        if (!dragState) return;
        const {rows, track, thumb, startY, startTop, maxScroll, maxThumbTop} = dragState;
        const deltaY = e.clientY - startY;
        const newTop = clamp(startTop + deltaY, 0, maxThumbTop);
        thumb.style.top = newTop + 'px';
        if (maxThumbTop > 0 && maxScroll > 0) {
            rows.scrollTop = (newTop / maxThumbTop) * maxScroll;
        }
    }

    function onDocMouseUp() {
        if (!dragState) return;
        const {track} = dragState;
        track.classList.remove('is-dragging');
        dragState = null;
        document.removeEventListener('mousemove', onDocMouseMove);
        document.removeEventListener('mouseup', onDocMouseUp);
    }

    cards.querySelectorAll('.thresholds-card').forEach(card => {
        const rows = card.querySelector('.thresholds-rows');
        const track = card.querySelector('.thresholds-scroll-track');
        const thumb = track ? track.querySelector('.thresholds-scroll-thumb') : null;
        if (!rows || !track || !thumb) return;

        rows.addEventListener('scroll', () => thresholdsSyncCard(rows, track, thumb));

        // Клик по дорожке — прыжок к месту клика
        track.addEventListener('mousedown', (e) => {
            if (e.target === thumb) return;
            if (track.classList.contains('is-idle')) return;
            const trackRect = track.getBoundingClientRect();
            const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
            if (maxScroll <= 0) return;
            const trackHeight = track.clientHeight;
            const thumbHeight = thumb.offsetHeight || 22;
            const maxThumbTop = Math.max(0, trackHeight - thumbHeight);
            const clickY = e.clientY - trackRect.top;
            const desiredTop = clamp(clickY - thumbHeight / 2, 0, maxThumbTop);
            thumb.style.top = desiredTop + 'px';
            rows.scrollTop = maxThumbTop > 0 ? (desiredTop / maxThumbTop) * maxScroll : 0;
        });

        // Перетаскивание бегунка
        thumb.addEventListener('mousedown', (e) => {
            if (track.classList.contains('is-idle')) return;
            e.preventDefault();
            e.stopPropagation();
            const trackHeight = track.clientHeight;
            const thumbHeight = thumb.offsetHeight || 22;
            const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
            const maxThumbTop = Math.max(0, trackHeight - thumbHeight);
            const currentTop = parseFloat(thumb.style.top) || 0;
            dragState = {
                rows, track, thumb,
                startY: e.clientY,
                startTop: currentTop,
                maxScroll,
                maxThumbTop,
            };
            track.classList.add('is-dragging');
            document.addEventListener('mousemove', onDocMouseMove);
            document.addEventListener('mouseup', onDocMouseUp);
        });

        track.addEventListener('wheel', (e) => {
            if (track.classList.contains('is-idle')) return;
            e.preventDefault();
            rows.scrollTop += e.deltaY;
        }, {passive: false});
    });

    updateCardVisibility();
}

// Ползунок виден только при переполнении. Бегунок: верх = начало, низ = конец.
function thresholdsSyncCard(rows, track, thumb) {
    if (!rows || !track || !thumb) return;
    if (!thumb) thumb = track.querySelector('.thresholds-scroll-thumb');
    if (!thumb) return;

    const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
    if (maxScroll <= 0) {
        track.classList.add('is-idle');
        thumb.style.top = '0px';
        if (rows.scrollTop) rows.scrollTop = 0;
        return;
    }
    track.classList.remove('is-idle');
    const trackHeight = track.clientHeight || 56;
    const ratio = rows.clientHeight / Math.max(1, rows.scrollHeight);
    const thumbHeight = Math.max(
        22,
        Math.min(Math.round(trackHeight * 0.6), Math.round(trackHeight * ratio)),
    );
    const maxThumbTop = Math.max(0, trackHeight - thumbHeight);
    const top = maxScroll > 0 ? (rows.scrollTop / maxScroll) * maxThumbTop : 0;
    thumb.style.height = thumbHeight + 'px';
    thumb.style.top = top + 'px';
}

// Синхронизация ползунка активной карточки (после показа панели,
// смены карточки или изменения размеров окна).
function thresholdsSyncScroll() {
    const body = els.thresholdsBody;
    if (!body) return;
    const card = body.querySelector('.thresholds-card.is-active');
    if (!card) return;
    const rows = card.querySelector('.thresholds-rows');
    const track = card.querySelector('.thresholds-scroll-track');
    const thumb = track ? track.querySelector('.thresholds-scroll-thumb') : null;
    thresholdsSyncCard(rows, track, thumb);
}

// Держим активную вкладку в зоне видимости ленты: при переключении
// правил (в т.ч. кликом по частично скрытой вкладке) подкручиваем
// ленту по горизонтали; страница по вертикали не двигается.
function thresholdsScrollActiveTabIntoView() {
    const body = els.thresholdsBody;
    if (!body) return;
    const tabs = body.querySelector('.thresholds-tabs');
    const activeTab = tabs && tabs.querySelector('.thresholds-tab.is-active');
    if (!tabs || !activeTab) return;
    const left = activeTab.offsetLeft;
    const right = left + activeTab.offsetWidth;
    if (left < tabs.scrollLeft) {
        tabs.scrollLeft = left;
    } else if (right > tabs.scrollLeft + tabs.clientWidth) {
        tabs.scrollLeft = right - tabs.clientWidth;
    }
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
            if (!event.target.matches('input.thresholds-input')) return;
            thresholdsDirty = true;
            // Если оператор вернул значения как было (0.3 -> 0.5 -> 0.3),
            // изменений больше нет — снимаем блокировку автоподхвата.
            if (!hasChangedThresholds()) thresholdsDirty = false;
            updateThresholdsActions();
        };
        // ``input`` покрывает набор с клавиатуры, ``change`` —
        // автозаполнение number-поля в браузерах, где input приходит поздно.
        els.thresholdsBody.addEventListener('input', markThresholdsChanged);
        els.thresholdsBody.addEventListener('change', markThresholdsChanged);
    }
    // При изменении размеров окна высота списка меняется — ползунок
    // прокрутки пересчитывается, а расширенная активная вкладка
    // досылается в зону видимости ленты вкладок.
    window.addEventListener('resize', () => {
        thresholdsSyncScroll();
        thresholdsScrollActiveTabIntoView();
    });
    updateThresholdsPanel();
}
