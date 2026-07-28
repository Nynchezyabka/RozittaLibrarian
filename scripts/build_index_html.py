"""
Сборка статического index.html для Rozitta Librarian.

Берёт макет UI из /home/z/my-project/upload/librarian_ui_макет.html
и заменяет в нём блок <script> на реальную реализацию экранов 1 и 2,
работающую через WebSocket с бэкендом Rozitta Librarian.

Экраны 3 (поиск) и 4 (ридер) оставлены визуально как в макете, но на
mock-данных — это следующий заход (UI-3, UI-4 по спецификации §10).

Скрипт идемпотентен — его можно перезапускать после изменений в макете.
"""
from pathlib import Path
import re

SRC_MOCK = Path("/home/z/my-project/upload/librarian_ui_макет.html")
DST = Path("/home/z/my-project/rozitta_librarian/static/index.html")

# ---------------------------------------------------------------------------
# Новый <script> блок — реальная реализация экранов 1 и 2.
# Экраны 3 и 4 — fallback на mock-данные (просто показываем заглушку).
# ---------------------------------------------------------------------------

SCRIPT_REPLACE_FROM = "<script>\n/* ================= МОК-ДАННЫЕ ================= */"
SCRIPT_REPLACE_TO = "</script>"

NEW_SCRIPT = r"""<script>
/* =================================================================
 * Rozitta Librarian — UI Этап 1 (реализация UI-1 + UI-2)
 * Спецификация: /home/z/my-project/upload/librarian_ui_спецификация_этап1.md
 *
 * Подключение к бэкенду: WebSocket → ws://<host>/ws
 *   Операции: list_archives, scan_archives, open_archive,
 *             list_shelves, stats, whats_new
 *             (search, read_post — пока mock для экранов 3 и 4)
 * ================================================================= */

const $ = id => document.getElementById(id);
const esc = s => (s||'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

/* ================= СОСТОЯНИЕ ================= */
const S = {
  ws: null,
  wsConnected: false,
  archives: [],            // список архивов (массив карточек)
  archive: null,           // текущий открытый архив (карточка)
  archiveFull: null,       // полный паспорт (для chat_id и т.п.)
  view: 'home',            // home | start | shelf | results | reader
  shelf: null,
  shelves: [],             // ответ list_shelves
  statsData: null,         // ответ stats
  whatsNew: [],            // ответ whats_new
  query: '', results: [],  // для UI-3 (mock-сценарий)
  backCtx: null,           // { label, restore() }
  readerItem: null,
  rzShown: {},
  // Mock-данные для UI-3/UI-4 — чтобы визуально показать, как будет выглядеть.
  // Реальные данные подключим в следующем заходе.
  mock: null,
};

/* ================= MOCK для UI-3/UI-4 (временно) =================
 * Это тот же mock, что в макете, но только для экранов 3 и 4.
 * На экранах 1 и 2 — реальные данные с бэкенда.
 */
S.mock = {
  phil: {
    id: 'phil', emoji: '📚', name: 'Философия буднего дня',
    handle: '@philosophy_daily', type: 'канал', dates: '15 сен — 1 окт 2024',
    chips: ['обесценивание', 'ценности', 'интерфейс'],
    items: [
      { id: 1, shelf: 'messages', date: '15 сен 2024, 09:10', d: '2024-09-15', author: '@philosophy_daily',
        text: 'Ценности не выбирают на полке, как товар. Их обнаруживают задним числом — по тому, на что мы тратим внимание, когда никто не проверяет. Посмотри на свой вчерашний день: вот твой честный список ценностей.',
        comments: [
          { author: '@vassa_k', date: '15 сен 2024, 12:40', text: 'Список ценностей из вчерашнего дня — отрезвляющее упражнение, спасибо.' }
        ] },
      { id: 2, shelf: 'messages', date: '18 сен 2024, 11:47', d: '2024-09-18', author: '@philosophy_daily',
        text: 'Язык — это интерфейс между нами и миром. Плохой интерфейс скрывает функциональность; бедный словарь скрывает оттенки опыта. Учить новые слова — значит расширять то, что вообще можно заметить.' },
      { id: 8, shelf: 'messages', voice: true, rec: 7, date: '25 сен 2024, 18:55', d: '2024-09-25', author: '@philosophy_daily',
        text: 'Аудио-эфир «Ценности и цена», 42 минуты. Слушайте в записи — или читайте расшифровку.' },
      { id: 7, shelf: 'records', src: 8, date: '25 сен 2024, 19:00', d: '2024-09-25', author: '@philosophy_daily',
        text: 'Всем привет, это запись эфира про ценности и цену. Мы часто путаем эти два слова, а между тем цена — то, что мы платим, а ценность — то, что получаем. Обесценивание начинается там, где мы перестаём различать одно и другое.\n\n[фрагмент расшифровки, полная запись — 42 минуты]' },
      { id: 3, shelf: 'messages', date: '28 сен 2024, 14:22', d: '2024-09-28', author: '@philosophy_daily',
        text: 'Проблема обесценивания труда в современных условиях не в том, что платят мало. Она в том, что результат труда перестал быть видимым: закрытая задача исчезает в трекере, а сложенная стена стоит десятилетиями. Невидимое легко обесценить.',
        comments: [
          { author: '@vassa_k', date: '28 сен 2024, 15:10', text: 'Очень точно про невидимость результата. У нас помогло еженедельное демо — труд снова стал видимым.' },
          { author: '@dim5x', date: '28 сен 2024, 16:02', text: 'А что делать с трудом, который невидим по своей природе — поддержка, инфраструктура?' }
        ] },
      { id: 4, shelf: 'messages', date: '29 сен 2024, 10:05', d: '2024-09-29', author: '@philosophy_daily',
        text: 'Обесценивание чужого опыта начинается со слов «да это же просто». Просто — для того, кто уже прошёл путь. Уважение к сложности чужого пути — минимальная форма честности.' },
      { id: 5, shelf: 'messages', date: '30 сен 2024, 16:30', d: '2024-09-30', author: '@philosophy_daily',
        text: 'Привычка — это решение, принятое один раз и исполняемое бесплатно. Сила воли дорога, привычки дёшевы.' },
      { id: 6, shelf: 'messages', date: '1 окт 2024, 12:00', d: '2024-10-01', author: '@philosophy_daily',
        text: 'Итоги сентябрьского цикла: говорили про ценности, интерфейсы восприятия и обесценивание. Общая нить — внимание.' },
    ]
  },
  pyforum: {
    id: 'pyforum', emoji: '💬', name: 'Tech Forum — Python',
    handle: '@pyforum', type: 'группа', dates: '10 фев — 5 мар 2025',
    chips: ['асинхронность', 'типизация'],
    items: [
      { id: 11, shelf: 'messages', date: '10 фев 2025, 13:15', d: '2025-02-10', author: '@dim5x',
        text: 'Коллеги, кто уже переводил большой проект на строгую типизацию? Интересует, насколько mypy в strict-режиме ловит реальные баги.' },
      { id: 12, shelf: 'messages', date: '14 фев 2025, 18:40', d: '2025-02-14', author: '@vassa_k',
        text: 'Переводили. Типизация окупилась при первом же рефакторинге: mypy подсветил все места, где поменялась сигнатура.' },
      { id: 13, shelf: 'messages', date: '20 фев 2025, 09:02', d: '2025-02-20', author: '@dim5x',
        text: 'Вопрос про асинхронность: FastAPI + SQLite. Есть ли смысл в async-драйвере, если база локальная?' },
      { id: 14, shelf: 'messages', date: '5 мар 2025, 21:11', d: '2025-03-05', author: '@vassa_k',
        text: 'Для локальной однопользовательской базы асинхронность драйвера почти ничего не даёт — узкое место не там.' },
    ]
  }
};

/* ================= WEBSOCKET ================= */
function wsConnect(){
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const url = `${proto}//${location.host}/ws`;
  S.ws = new WebSocket(url);

  S.ws.onopen = () => {
    S.wsConnected = true;
    $('conn-warn').style.display = 'none';
    devLog('WebSocket: подключён');
    // На старте — сразу получить список архивов для экрана 1
    wsSend({op: 'list_archives'});
  };

  S.ws.onclose = () => {
    S.wsConnected = false;
    $('conn-warn').style.display = 'inline-flex';
    devLog('WebSocket: соединение закрыто', 'warn');
    // Попытка переподключения через 2 сек
    setTimeout(() => { if (!S.wsConnected) wsConnect(); }, 2000);
  };

  S.ws.onerror = (e) => {
    devLog('WebSocket: ошибка', 'error');
  };

  S.ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    handleWsMessage(msg);
  };
}

function wsSend(payload){
  if (S.ws && S.ws.readyState === WebSocket.OPEN){
    S.ws.send(JSON.stringify(payload));
  } else {
    devLog('WS не готов — сообщение потеряно', 'error');
  }
}

/* ================= ОБРАБОТКА WS-СООБЩЕНИЙ ================= */
const _pending = {}; // op → callback (для упрощённой request/response поверх потокового WS)

function handleWsMessage(msg){
  if (msg.type === 'hello'){
    devLog(`hello: ${msg.message} (порт ${msg.port})`);
    return;
  }
  if (msg.type === 'log'){
    devLog(msg.message, msg.level === 'success' ? 'ok' : msg.level === 'error' ? 'error' : msg.level === 'warning' ? 'warn' : '');
    return;
  }
  if (msg.type === 'error'){
    devLog(`ошибка: ${msg.message}`, 'error');
    toast(msg.message);
    return;
  }
  if (msg.type === 'result'){
    handleResult(msg.op, msg.data);
    return;
  }
}

function handleResult(op, data){
  if (op === 'list_archives' || op === 'scan_archives'){
    S.archives = data.archives || [];
    renderHome();
    if (op === 'scan_archives'){
      toast(`Проверила папку output — найдено архивов: ${S.archives.length}`);
    }
    return;
  }
  if (op === 'open_archive'){
    // data = { card, passport }
    S.archive = data.card;
    S.archiveFull = data.passport;
    // После открытия архива — параллельно получить полки, сводку, последние
    if (S.archive){
      wsSend({op: 'list_shelves', archive_id: S.archive.id});
      wsSend({op: 'stats',        archive_id: S.archive.id});
      wsSend({op: 'whats_new',    archive_id: S.archive.id, args: {limit: 3}});
    }
    return;
  }
  if (op === 'list_shelves'){
    S.shelves = data.shelves || data || [];
    renderSidebarAfterOpen();
    return;
  }
  if (op === 'stats'){
    S.statsData = data;
    renderSidebarAfterOpen();
    return;
  }
  if (op === 'whats_new'){
    S.whatsNew = data.items || data.posts || data.recent || [];
    // Если ждали полку — отрисуем полку, иначе стартовую страницу
    if (S._pendingShelf){
      renderShelfFromWhatsNew(S._pendingShelf);
      S._pendingShelf = null;
    } else {
      renderStart();
    }
    return;
  }
  devLog(`op=${op} — нет обработчика, данные в логе`, 'warn');
  devLog(JSON.stringify(data).slice(0, 200));
}

/* ================= РОЗИТТА ================= */
function rzSay(where, key, text){
  if (S.rzShown[key]) return;
  const bubble = $(where === 'home' ? 'rz-home-bubble' : 'rz-side-bubble');
  const span   = $(where === 'home' ? 'rz-home-text'   : 'rz-side-text');
  if (!bubble || !span) return;
  span.textContent = text;
  bubble.style.display = 'block';
  bubble.dataset.key = key;
}
function rzClose(where){
  const bubble = $(where === 'home' ? 'rz-home-bubble' : 'rz-side-bubble');
  if (!bubble) return;
  if (bubble.dataset.key) S.rzShown[bubble.dataset.key] = true;
  bubble.style.display = 'none';
}

/* ================= РОУТИНГ ================= */
window.addEventListener('hashchange', route);
window.addEventListener('DOMContentLoaded', () => {
  if (new URLSearchParams(location.search).get('debug') === '1') devToggle(true);
  $('crumb-home').onclick = () => nav('#/');
  wsConnect();
  route();
});

function route(){
  const raw = location.hash.replace(/^#\/?/, '');
  const [pathPart, queryPart] = raw.split('?');
  const p = pathPart.split('/').filter(Boolean);
  if (!p.length) { show('home'); return; }
  if (p[0] === 'a' && p[1]){
    if (!S.archive || S.archive.id !== p[1]){
      // Архив ещё не открыт — запросим открытие
      openArchive(p[1], /*navigate=*/false);
      // После открытия route() вызовется снова через renderStart / renderShelf / renderReader
      // А пока — покажем archive-скрин как загружающийся
      show('archive');
      return;
    }
    if (p[2] === 'shelf' && p[3])      renderShelf(p[3]);
    else if (p[2] === 'search')        {
      const q = new URLSearchParams(queryPart||'').get('q')||'';
      if (q){ $('q-input').value = q; runSearch(q); }
      else renderStart();
    }
    else if (p[2] === 'm' && p[3])     renderReader(parseInt(p[3],10));
    else                               renderStart();
    return;
  }
  show('home');
}
function nav(hash){ if (location.hash !== hash) location.hash = hash; else route(); }
function go(view, arg){
  if (!S.archive) return;
  const a = S.archive.id;
  if (view === 'start') nav(`#/a/${a}`);
  if (view === 'shelf') nav(`#/a/${a}/shelf/${arg}`);
}

/* ================= ЭКРАНЫ ================= */
function show(screen){
  S.view = screen;
  $('screen-home').classList.toggle('active', screen === 'home');
  $('screen-archive').classList.toggle('active', screen !== 'home');
  $('crumb-home').classList.toggle('visible', screen !== 'home');
}
function showView(v){
  ['start','shelf','results','reader'].forEach(x => $('view-'+x).classList.toggle('active', x === v));
  document.querySelectorAll('.toc-item').forEach(el => el.classList.remove('active'));
  if (v === 'start') document.querySelector('[data-nav="start"]')?.classList.add('active');
  if (v === 'shelf') document.querySelector(`[data-nav="shelf-${S.shelf}"]`)?.classList.add('active');
}

/* ---- Экран 1: список архивов (РЕАЛЬНЫЕ ДАННЫЕ) ---- */
function renderHome(){
  const grid = $('arch-grid');
  if (!S.archives.length){
    grid.innerHTML = `<div style="grid-column:1/-1;padding:40px 20px;text-align:center;color:var(--text-ter);font-size:0.84rem;">
      <div style="font-size:2rem;margin-bottom:10px;">🐸</div>
      Я не нашла ни одного архива.<br>
      Скопируй папку из Rozitta Parser в <code>output/</code> и нажми «Обновить».
    </div>`;
    rzSay('home','empty','Я не нашла ни одного архива. Скопируй папку из Rozitta Parser в output и обновите страницу.');
    return;
  }
  grid.innerHTML = S.archives.map(a => {
    const m = a.messages_count || 0;
    const r = a.transcriptions_count || 0;
    const mWord = m === 1 ? 'сообщение' : m < 5 ? 'сообщения' : 'сообщений';
    const rWord = r === 1 ? 'запись' : r < 5 ? 'записи' : 'записей';
    const handle = a.username || '—';
    return `<div class="arch-card" onclick="nav('#/a/${esc(a.id)}')">
      <div class="ac-emoji">${a.emoji || '📦'}</div>
      <div class="ac-name">${esc(a.title)}</div>
      <div class="ac-handle"><code>${esc(handle)}</code> · ${esc(a.type_label || a.chat_type || '')}</div>
      <div class="ac-counts">${m} ${mWord}${r ? ` · ${r} ${rWord}` : ''}</div>
      <div class="ac-dates">${esc(a.date_period || '')}</div>
      <div class="ac-open">Открыть →</div>
    </div>`;
  }).join('');
  rzSay('home','home-hint','Выбери архив — и я помогу найти в нём что угодно.');
}

/* ---- Открытие архива (РЕАЛЬНЫЕ ДАННЫЕ) ---- */
function openArchive(id, navigate=true){
  S.archive = null; S.shelves = []; S.statsData = null; S.whatsNew = [];
  show('archive');
  wsSend({op: 'open_archive', archive_id: id});
  if (navigate) nav(`#/a/${id}`);
}

function renderSidebarAfterOpen(){
  // Вызывается после list_shelves и stats (могут приходить в любом порядке)
  if (!S.archive) return;
  const a = S.archive;
  $('side-emoji').textContent = a.emoji || '📦';
  $('side-name').textContent = a.title || a.id;

  // Полки из ответа list_shelves — это массив {kind, label, count}
  // По спецификации полки = "messages" (Сообщения) и "transcriptions" (Записи)
  let m = 0, r = 0;
  if (Array.isArray(S.shelves)){
    for (const sh of S.shelves){
      if (sh.kind === 'messages') m = sh.count || 0;
      if (sh.kind === 'transcriptions') r = sh.count || 0;
    }
  }
  // Fallback на паспортные счётчики, если list_shelves ещё не пришёл
  if (m === 0 && a.messages_count) m = a.messages_count;
  if (r === 0 && a.transcriptions_count) r = a.transcriptions_count;

  $('cnt-messages').textContent = m;
  $('cnt-records').textContent = r;
  $('toc-records').style.display = r ? 'flex' : 'none';

  // Сводка — частично из карточки, частично из stats
  $('sum-handle').textContent = a.username || '—';
  $('sum-type').textContent = a.type_label || a.chat_type || '—';
  $('sum-dates').textContent = a.date_period || '—';
  $('sum-msg').textContent = m;
  $('sum-rec').textContent = r;

  // Чипы — из паспорта (поле chips)
  const chips = a.chips || [];
  $('chips').innerHTML = chips.length
    ? chips.map(c => `<span class="chip" onclick="chipSearch('${esc(c).replace(/'/g,"\\'")}')">${esc(c)}</span>`).join(' ')
    : '<span style="color:var(--text-ter);font-size:0.72rem;">(нет примеров — начни печатать слово)</span>';
}

function renderStart(){
  if (!S.archive) return;
  const a = S.archive;
  $('start-title').textContent = a.title;
  const m = a.messages_count || 0;
  const r = a.transcriptions_count || 0;
  $('start-sub').textContent = `${a.type_label || a.chat_type} · ${m} сообщений${r?` · ${r} запись`:''} · ${a.date_period || ''}`;

  // Последние сообщения — из whats_new (limit=3)
  const recent = S.whatsNew || [];
  if (recent.length){
    $('start-recent').innerHTML = recent.map(i => msgCardReal(i, 'start')).join('');
  } else {
    $('start-recent').innerHTML = `<div style="color:var(--text-ter);padding:20px 0;font-size:0.8rem;">Загружаю последние сообщения…</div>`;
  }
  showView('start');
  $('q-input').focus();
  rzSay('side','first-open','Начни с поиска или загляни на полку слева. Не знаешь, что искать — попробуй слова под строкой поиска.');
}

/* Карточка сообщения из РЕАЛЬНОГО ответа whats_new
 * Структура whats_new: { items: [{ internal_id, chat_id, message_id, author, date, text_preview, url }, ...] }
 * Приводим к виду, удобному для UI.
 */
function msgCardReal(item, from='start'){
  const id   = item.message_id || item.id || '?';
  const text = item.text_preview || item.text || '';
  const date = item.date || '';
  const author = item.author || item.username || '';
  // whats_new не отличает сообщения от записей — на UI-3/UI-4 добавим флаг
  // через get_message. Пока считаем всё сообщением.
  const isRec = false;
  const preview = text.length > 150 ? text.slice(0, 150) + '…' : text;
  return `<div class="msg-card" onclick="openReaderMock(${id},'${from}')" title="Читать (UI-4 — следующий заход)">
    <div class="mc-head">
      <span class="mc-id">#${esc(id)}</span>
      <span class="mc-date">${esc(date)}</span>
      <span class="mc-shelf ${isRec?'rec':''}">${isRec?'🎙 запись':'💬 сообщение'}</span>
    </div>
    <div class="mc-text">${esc(preview) || '<em>(без текста)</em>'}</div>
    <div class="mc-read">Читать →</div>
  </div>`;
}

/* ---- Экран 2: полка (РЕАЛЬНЫЕ ДАННЫЕ через whats_new с большим лимитом) ----
 * Ограничение: whats_new сейчас не фильтрует по shelf. Для полки "messages"
 * это ок (возвращаются последние N сообщений). Для полки "records" — будут
 * показаны сообщения вперемешку. Когда добавим операцию list_shelf_items
 * (или расширим whats_new параметром shelf) — заменим.
 */
function renderShelf(shelf){
  if (!S.archive) return;
  S.shelf = shelf;
  wsSend({op: 'whats_new', archive_id: S.archive.id, args: {limit: 200}});
  $('shelf-title').textContent = shelf === 'messages' ? '💬 Сообщения' : '🎙 Записи';
  $('shelf-sub').textContent = 'Загружаю…';
  $('shelf-list').innerHTML = '';
  showView('shelf');
  S._pendingShelf = shelf;
}

function renderShelfFromWhatsNew(shelf){
  const items = S.whatsNew || [];
  const n = items.length;
  $('shelf-title').textContent = shelf === 'messages' ? '💬 Сообщения' : '🎙 Записи';
  $('shelf-sub').textContent = `${n} на этой полке · сначала новые`;
  $('shelf-list').innerHTML = n
    ? items.map(i => msgCardReal(i, 'shelf')).join('')
    : '<div style="color:var(--text-ter);padding:20px 0;font-size:0.8rem;">На этой полке ничего нет.</div>';
  showView('shelf');
}

/* ================= UI-3/UI-4: ПОИСК И РИДЕР (mock) =================
 * Пока не реализованы на бэке полностью (нет get_message, нет расширенной
 * индексации комментариев), используем mock-данные из макета.
 * Когда сделаем UI-3 и UI-4 — заменим на wsSend({op:'search', ...}) и
 * wsSend({op:'get_message', ...}).
 */
function tokenize(q){ return q.toLowerCase().split(/\s+/).filter(t => t.length >= 2); }
function matches(text, tokens){
  const words = (text||'').toLowerCase().split(/[^а-яa-zё0-9]+/i);
  return tokens.every(t => words.some(w => w.startsWith(t)));
}
function highlight(text, tokens){
  if (!tokens.length) return esc(text);
  const re = new RegExp('(^|[^а-яa-zё0-9])(' + tokens.map(t => t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|') + ')([а-яa-zё0-9]*)','gi');
  return esc(text).replace(re, (m,pre,tok,rest) => pre + '<mark>' + tok + rest + '</mark>');
}
function snippet(text, tokens, len=170){
  const lower = (text||'').toLowerCase();
  let pos = 0;
  for (const t of tokens){ const p = lower.indexOf(t); if (p >= 0){ pos = p; break; } }
  let start = Math.max(0, pos - 60);
  let cut = (text||'').slice(start, start + len);
  return (start > 0 ? '…' : '') + cut + (start + len < (text||'').length ? '…' : '');
}

function toggleAdv(){
  $('adv-panel').classList.toggle('open');
  $('adv-toggle').classList.toggle('open');
}
function chipSearch(q){ $('q-input').value = q; doSearch(); }
$('q-input').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });

function doSearch(){
  const q = $('q-input').value.trim();
  if (!q) return;
  nav(`#/a/${S.archive.id}/search?q=${encodeURIComponent(q)}`);
}

function runSearch(q){
  // Mock-поиск: данные берём из S.mock по id архива.
  // Когда сделаем UI-3 — заменим на wsSend({op:'search', archive_id, args:{query:q, ...}})
  const mockDb = S.mock[S.archive.id];
  if (!mockDb){
    $('results-count').innerHTML = '';
    $('results-list').innerHTML = `<div class="no-results"><div class="big">🚧</div>
      Поиск будет доступен после реализации UI-3.<br>
      Сейчас mock-поиск работает только для демо-архивов.</div>`;
    showView('results');
    return;
  }
  const tokens = tokenize(q);
  const res = mockDb.items.filter(i => matches(i.text, tokens) || (i.comments||[]).some(c => matches(c.text, tokens)));
  S.query = q; S.results = res;
  devLog(`search (mock): "${q}" → ${res.length} hits`);
  if (!res.length){
    $('results-count').innerHTML = '';
    $('results-list').innerHTML = `<div class="no-results"><div class="big">🐸</div>
      По слову «${esc(q)}» я ничего не нашла.<br>Попробуй начало слова или другой корень — я ищу по началу слов.</div>`;
  } else {
    const n = res.length;
    $('results-count').innerHTML = `Найдено: <b>${n}</b> совпадени${n===1?'е':n<5?'я':'й'} по запросу «${esc(q)}» <span style="color:var(--text-ter);font-size:0.74rem;">(mock — UI-3 в разработке)</span>`;
    $('results-list').innerHTML = res.map(i => msgCardMock(i, tokens, 'results')).join('');
    rzSay('side','first-hit','Нашла! Нажми «Читать», чтобы открыть сообщение целиком.');
  }
  showView('results');
}

function msgCardMock(item, tokens=[], from='start'){
  const isRec = item.shelf === 'records';
  let srcText = item.text, comBadge = '';
  if (tokens.length && !matches(item.text, tokens)){
    const c = (item.comments||[]).find(c => matches(c.text, tokens));
    if (c){ srcText = c.text; comBadge = `<span class="mc-shelf com">↳ в комментарии</span>`; }
  }
  const body = tokens.length ? highlight(snippet(srcText, tokens), tokens) : esc(item.text.slice(0,150)) + (item.text.length>150?'…':'');
  return `<div class="msg-card" onclick="openReaderMock(${item.id},'${from}')">
    <div class="mc-head">
      <span class="mc-id">#${item.id}</span>
      <span class="mc-date">${esc(item.date)}</span>
      ${comBadge}
      <span class="mc-shelf ${isRec?'rec':''}">${isRec?'🎙 запись':item.voice?'🎙 аудио':'💬 сообщение'}</span>
    </div>
    <div class="mc-text">${body}</div>
    <div class="mc-read">Читать →</div>
  </div>`;
}

function openReaderMock(id, from){
  const view = document.querySelector('.view.active');
  const scroll = view ? view.scrollTop : 0;
  if (from === 'results'){
    const q = S.query, res = S.results;
    S.backCtx = { label: `← К результатам («${q}», ${res.length})`,
      restore(){ nav(`#/a/${S.archive.id}/search?q=${encodeURIComponent(q)}`); setTimeout(()=> $('view-results').scrollTop = scroll, 0); } };
  } else if (from === 'shelf'){
    const sh = S.shelf;
    S.backCtx = { label: '← К полке',
      restore(){ nav(`#/a/${S.archive.id}/shelf/${sh}`); setTimeout(()=> $('view-shelf').scrollTop = scroll, 0); } };
  } else {
    S.backCtx = { label: '← К обзору', restore(){ nav(`#/a/${S.archive.id}`); } };
  }
  nav(`#/a/${S.archive.id}/m/${id}`);
}

function renderReader(id){
  // Mock-ридер: данные из S.mock
  const mockDb = S.mock[S.archive ? S.archive.id : ''];
  if (!mockDb){
    $('rd-title').textContent = '🚧 UI-4 — следующий заход';
    $('rd-author').textContent = '—';
    $('rd-date').textContent = '—';
    $('rd-body').innerHTML = `<p style="color:var(--text-sec);">Ридер будет подключён к реальному бэкенду после реализации операции <code>get_message</code> (спецификация §9).</p>`;
    $('rd-tg').style.display = 'none';
    $('rd-linked').innerHTML = '';
    $('rd-comments').innerHTML = '';
    $('rn-prev').disabled = true; $('rn-next').disabled = true;
    $('reader-back').textContent = S.backCtx ? S.backCtx.label : '← Назад';
    showView('reader');
    return;
  }
  const item = mockDb.items.find(i => i.id === id);
  if (!item) return;
  S.readerItem = item;
  const isRec = item.shelf === 'records';
  const tokens = (S.query && S.results.some(r=>r.id===id)) ? tokenize(S.query) : [];
  $('reader-back').textContent = S.backCtx ? S.backCtx.label : '← К обзору';
  $('rd-title').textContent = (isRec ? 'Запись' : 'Сообщение') + ' #' + item.id;
  $('rd-recplate').style.display = isRec ? 'inline-flex' : 'none';
  $('rd-ava').textContent = (item.author||'@?').replace('@','')[0].toUpperCase();
  $('rd-author').textContent = item.author;
  $('rd-date').textContent = item.date;
  $('rd-body').innerHTML = highlight(item.text, tokens);
  $('rd-tg').style.display = isRec ? 'none' : 'inline-flex';
  const handle = (mockDb.handle||'').replace('@','');
  $('rd-tg').href = `https://t.me/${handle}/${item.id}`;
  let links = '';
  if (item.rec) links += `<button class="link-plate" onclick="openReaderMock(${item.rec},'results')">🎙 Есть расшифровка — читать</button>`;
  if (item.src) links += `<button class="link-plate msg" onclick="openReaderMock(${item.src},'results')">💬 Исходное аудио-сообщение #${item.src}</button>`;
  $('rd-linked').innerHTML = links;
  const coms = item.comments || [];
  $('rd-comments').innerHTML = coms.length
    ? `<div class="rd-com-h">💬 Комментарии (${coms.length})</div>` + coms.map(c =>
        `<div class="comment-row"><span class="c-author">${esc(c.author)}</span><span class="c-date">${esc(c.date)}</span><div class="c-text">${highlight(c.text, tokens)}</div></div>`).join('')
    : '';
  const shelfItems = mockDb.items.filter(i=>i.shelf===item.shelf).sort((x,y)=> x.d.localeCompare(y.d));
  const idx = shelfItems.findIndex(i=>i.id===item.id);
  const prev = shelfItems[idx-1], next = shelfItems[idx+1];
  $('rn-prev').disabled = !prev; $('rn-next').disabled = !next;
  $('rn-prev-t').textContent = prev ? `#${prev.id} · ${prev.date.split(',')[0]}` : '';
  $('rn-next-t').textContent = next ? `#${next.id} · ${next.date.split(',')[0]}` : '';
  $('rn-prev').dataset.id = prev?.id ?? ''; $('rn-next').dataset.id = next?.id ?? '';
  showView('reader');
  $('view-reader').scrollTop = 0;
  const firstMark = $('rd-body').querySelector('mark');
  if (firstMark) firstMark.scrollIntoView({block:'center'});
}
function rdNav(dir){
  const id = $(dir<0?'rn-prev':'rn-next').dataset.id;
  if (id) nav(`#/a/${S.archive.id}/m/${id}`);
}
function goBack(){
  if (S.backCtx) S.backCtx.restore();
  else if (S.archive) nav(`#/a/${S.archive.id}`);
  else nav('#/');
}

/* ================= ПАНЕЛЬ РАЗРАБОТЧИКА ================= */
let logoClicks = 0, logoTimer = null;
$('logo').addEventListener('click', () => {
  logoClicks++;
  clearTimeout(logoTimer);
  logoTimer = setTimeout(()=> logoClicks = 0, 600);
  if (logoClicks >= 3){ logoClicks = 0; devToggle(); }
});
function devToggle(force){
  const p = $('devpanel');
  const open = force !== undefined ? force : !p.classList.contains('open');
  p.classList.toggle('open', open);
}
function devLog(msg, level=''){
  const t = new Date().toTimeString().slice(0,8);
  const cls = level === 'ok' ? 'ok' : level === 'error' ? 'err' : level === 'warn' ? 'warn' : '';
  const log = $('dp-log');
  if (!log) return;
  const span = `<span style="color:var(--text-ter)">${t}</span>`;
  const arr  = level === 'error' ? '<span style="color:var(--error)">✗</span>' :
               level === 'warn'  ? '<span style="color:var(--warning)">!</span>' :
                                   '<span style="color:var(--success)">›</span>';
  log.innerHTML += `<div>${span} ${arr} ${esc(msg)}</div>`;
  log.scrollTop = 9e9;
}
function dpExec(){
  const v = $('dp-op').value.trim();
  if (!v) return;
  // Простой парсер: "op archive_id {args json}"
  const m = v.match(/^(\w+)\s+(\S+)?\s*(\{.*\})?\s*$/);
  if (!m){
    devLog(`неверный формат: "op archive_id {json}"`, 'error');
    return;
  }
  const [, op, aid, argsJson] = m;
  let args = {};
  if (argsJson){ try { args = JSON.parse(argsJson); } catch { devLog('невалидный JSON', 'error'); return; } }
  wsSend({op, archive_id: aid || '', args});
  $('dp-op').value = '';
}
function dpCopy(){
  navigator.clipboard?.writeText($('dp-log').innerText).then(()=> toast('Лог скопирован'));
}

/* ================= ДОБАВЛЕНИЕ АРХИВА ================= */
function modalOpen(){ $('modal-add').classList.add('open'); }
function modalClose(){ $('modal-add').classList.remove('open'); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') modalClose(); });
function rescan(userFacing){
  wsSend({op: 'scan_archives'});
}
function toast(msg){
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(()=> t.classList.remove('show'), 2600);
}

/* Обновить dp-port и статус после hello */
window.addEventListener('load', () => {
  // Ничего — обработчик hello обновит
});
</script>"""

def build():
    src = SRC_MOCK.read_text(encoding="utf-8")
    # Найти начало <script>... и заменить всё вплоть до </script>
    # Ищем по уникальному комментарию в начале script-блока
    start_marker = "<script>\n/* ================= МОК-ДАННЫЕ ================= */"
    end_marker = "</script>"

    start = src.find(start_marker)
    if start < 0:
        raise SystemExit("Не нашёл start_marker в макете — макет изменился, обновите скрипт")
    # Найти </script> после start
    end = src.find(end_marker, start)
    if end < 0:
        raise SystemExit("Не нашёл </script> после start_marker")

    new_html = src[:start] + NEW_SCRIPT + src[end + len(end_marker):]

    # Также заменить <title> — это теперь не макет, а рабочий UI
    new_html = new_html.replace(
        "<title>Rozitta Librarian — макет UI (Этап 1)</title>",
        "<title>Rozitta Librarian — Читальный зал</title>",
    )

    # Заменить плашку в футере — это больше не «макет UI»
    new_html = new_html.replace(
        '<span class="acc">макет UI — данные демонстрационные</span>',
        '<span class="acc">этап 1 · экраны 1–2 — реальные данные, экраны 3–4 — заглушка</span>',
    )

    DST.write_text(new_html, encoding="utf-8")
    print(f"OK: {DST} ({len(new_html)} байт)")

if __name__ == "__main__":
    build()
