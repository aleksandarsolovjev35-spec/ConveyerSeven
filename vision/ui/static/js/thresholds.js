// thresholds.js — Line Monitor UI module
'use strict';

// Панель «Пороги правил» показывает параметры правил выбранной (главной)
// камеры с понятными русскими названиями (label приходит с сервера).
// Каждое правило — собственная компактная карточка; переключение между
// правилами — клик по заголовку-вкладке (как вкладки в браузере).
// Выбранное правило разворачивается: карточка показывает строки в полный
// рост; повторный клик по активной вкладке сворачивает её обратно.
// Значение каждого порога задаётся только числовым полем; отдельный
// вертикальный ползунок прокручивает строки карточки, когда они не
// помещаются, и полностью скрыт, когда видны все строки. Редактирование
// доступно только до пуска (IDLE) и после полной остановки (STOPPED) и
// блокируется на время реального движения ленты (jog.busy); backend
// дополнительно проверяет состояние при сохранении.

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

// ─── Блоки-карточки правил ─────────────────────────────────────────
// Каждое правило (категория) — отдельная компактная карточка. Сверху —
// лента вкладок-заголовков (как в браузере): активная вкладка связана с
// видимой карточкой и выделяется сдержанно — только обводкой. Все
// вкладки делят ширину ленты поровну и всегда умещаются целиком —
// переключение мышкой не требует прокрутки ленты; при наведении
// заголовок «разъезжается» и показывает полное название (системная
// подсказка title остаётся запасным вариантом). Клик по вкладке
// переключает видимую карточку и разворачивает её (строки в полный
// рост); повторный клик по активной вкладке сворачивает карточку
// обратно. У каждой карточки свой вертикальный ползунок: он появляется
// только когда строки карточки не помещаются по высоте; если все строки
// видны — ползунок полностью скрыт.

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

    // Вкладки-заголовки правил, как вкладки в браузере: одна вкладка на
    // правило, активная выделяется только обводкой. Все вкладки делят
    // ширину ленты поровну и умещаются целиком; при наведении заголовок
    // «разъезжается» и показывает полное название (системная подсказка
    // title — запасной вариант).
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
        const count = document.createElement('span');
        count.className = 'thresholds-tab-count';
        count.textContent = String((group.params || []).length);
        tab.appendChild(count);
        tab.addEventListener('click', () => {
            if (thresholdsCardIndex === index) {
                // Клик по уже выбранной вкладке: свернуть карточку
                // правила или развернуть её обратно.
                const card = cards.querySelector(
                    `.thresholds-card[data-index="${index}"]`,
                );
                if (card) {
                    card.classList.toggle('is-expanded');
                    syncTabHints();
                    thresholdsSyncScroll();
                    // После анимации сворачивания высота строк меняется —
                    // ползунок нужно пересчитать по конечному размеру.
                    window.setTimeout(thresholdsSyncScroll, 260);
                }
                return;
            }
            thresholdsCardIndex = index;
            updateCardVisibility();
        });
        tabs.appendChild(tab);
    });

    // Раскрытие заголовка по наведению: вкладка «разъезжается» до
    // полного названия, соседние вкладки уступают ширину. flex-basis:
    // auto CSS анимировать не умеет (auto — не длина), поэтому измеряем
    // полную ширину названия и подставляем её пикселями — раскрытие
    // получается плавным (transition на flex-basis в thresholds.css).
    // Сжатие вкладки остаётся разрешённым, так что лента никогда не
    // выходит за правый край. На устройствах без наведения (тачскрины)
    // лента не дёргается: полное название остаётся в подсказке title.
    const THRESHOLDS_TAB_GAP = 4;   // зазор между названием и счётчиком
    const THRESHOLDS_TAB_PAD = 14;  // поля 6+6 и рамки 1+1 вкладки
    const hoverCapable = !window.matchMedia
        || window.matchMedia('(hover: hover)').matches;
    tabs.addEventListener('pointerover', event => {
        if (!hoverCapable) return;
        const tab = event.target.closest('.thresholds-tab');
        if (!tab) return;
        const label = tab.querySelector('.thresholds-tab-label');
        const count = tab.querySelector('.thresholds-tab-count');
        const textWidth = label ? label.scrollWidth : 0;
        const countWidth = count ? count.offsetWidth : 0;
        tab.style.flexBasis = String(
            textWidth + THRESHOLDS_TAB_GAP + countWidth + THRESHOLDS_TAB_PAD,
        ) + 'px';
    });
    tabs.addEventListener('pointerout', event => {
        if (!hoverCapable) return;
        const tab = event.target.closest('.thresholds-tab');
        if (!tab) return;
        const next = event.relatedTarget;
        // Переход на элемент внутри вкладки (например, на счётчик)
        // раскрытие не отменяет; уход на другую вкладку или за пределы
        // ленты возвращает её к равным долям.
        if (next && tab.contains(next)) return;
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

        const slider = document.createElement('input');
        slider.type = 'range';
        slider.className = 'thresholds-scroll-slider thresholds-card-slider';
        slider.min = 0;
        slider.max = 1000;
        slider.step = 1;
        slider.value = 0;
        slider.disabled = true;
        slider.setAttribute('aria-label', 'Прокрутка карточки правил');
        slider.title = 'Прокрутка карточки правил';
        cardBody.appendChild(slider);

        card.appendChild(cardBody);
        cards.appendChild(card);
    });

    scroll.append(cards);
    body.appendChild(scroll);

    // Показ только активной карточки + состояние вкладок. Выбранное
    // правило разворачивается: активной карточке добавляется is-expanded,
    // при уходе с вкладки разворот снимается.
    const syncTabHints = () => {
        [...tabs.querySelectorAll('.thresholds-tab')].forEach((tab, index) => {
            const label = rules[index].label || rules[index].rule;
            if (index !== thresholdsCardIndex) {
                tab.title = label;
                return;
            }
            const card = cards.querySelector(
                `.thresholds-card[data-index="${index}"]`,
            );
            const expanded = !card || card.classList.contains('is-expanded');
            tab.title = expanded
                ? `${label} — свернуть карточку`
                : `${label} — развернуть карточку`;
        });
    };

    const updateCardVisibility = () => {
        const total = rules.length;
        thresholdsCardIndex = Math.max(0, Math.min(total - 1, thresholdsCardIndex));
        [...cards.querySelectorAll('.thresholds-card')].forEach((card, index) => {
            const isActive = index === thresholdsCardIndex;
            if (isActive && !card.classList.contains('is-active')) {
                card.classList.add('is-expanded');
            }
            if (!isActive) card.classList.remove('is-expanded');
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
        // Разворачивание анимировано: после завершения высоты строк
        // меняются — ползунок пересчитывается по конечному размеру,
        // а расширенная активная вкладка досылается в зону видимости.
        window.setTimeout(() => {
            thresholdsSyncScroll();
            thresholdsScrollActiveTabIntoView();
        }, 260);
    };

    // Ползунок каждой карточки прокручивает только её строки.
    cards.querySelectorAll('.thresholds-card').forEach(card => {
        const rows = card.querySelector('.thresholds-rows');
        const slider = card.querySelector('.thresholds-scroll-slider');
        if (!rows || !slider) return;
        rows.addEventListener('scroll', () => thresholdsSyncCard(rows, slider));
        slider.addEventListener('input', () => {
            const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
            if (maxScroll <= 0) return;
            rows.scrollTop = (Number(slider.value) || 0) / 1000 * maxScroll;
        });
    });

    updateCardVisibility();
}

// Синхронизация ползунка конкретной карточки с фактической прокруткой.
// Ползунок нужен, только когда строки карточки не помещаются; если все
// строки видны — он полностью скрыт (значение порогов он не задаёт).
// Положение бегунка повторяет прокрутку: верх шкалы — верх списка.
function thresholdsSyncCard(rows, slider) {
    if (!rows || !slider) return;
    const maxScroll = Math.max(0, rows.scrollHeight - rows.clientHeight);
    if (maxScroll <= 0) {
        slider.disabled = true;
        slider.value = 0;
        slider.classList.add('is-idle');
        if (rows.scrollTop) rows.scrollTop = 0;
        return;
    }
    slider.classList.remove('is-idle');
    slider.disabled = false;
    slider.value = Math.max(0, Math.min(1000,
        Math.round(rows.scrollTop / maxScroll * 1000)));
}

// Синхронизация ползунка активной карточки (после показа панели,
// смены карточки или изменения размеров окна).
function thresholdsSyncScroll() {
    const body = els.thresholdsBody;
    if (!body) return;
    const card = body.querySelector('.thresholds-card.is-active');
    if (!card) return;
    thresholdsSyncCard(
        card.querySelector('.thresholds-rows'),
        card.querySelector('.thresholds-scroll-slider'),
    );
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
