"""
Сборка статического index.html для Rozitta Librarian.

Берёт макет UI из /home/z/my-project/upload/librarian_ui_макет.html
и заменяет в нём блок <script> на реальную реализацию ВСЕХ экранов 1–4,
работающую через WebSocket с бэкендом Rozitta Librarian.

Экраны:
  1. Home — список архивов (list_archives / scan_archives)
  2. Archive start — оглавление + сводка + последние (open_archive + list_shelves + stats + whats_new)
  3. Search — результаты поиска (search), с расширенным фильтром и обработкой нуля
  4. Reader — полное сообщение (get_message): пост + комментарии + транскрипция + соседи + t.me

Чипы под строкой поиска — динамические, вычисляются бэкендом из FTS5
(top_terms), парсер их не хранит.

Скрипт идемпотентен — его можно перезапускать после изменений в макете.
"""
from pathlib import Path
import re

SRC_MOCK = Path("/home/z/my-project/upload/librarian_ui_макет.html")
DST = Path("/home/z/my-project/rozitta_librarian/static/index.html")

# ---------------------------------------------------------------------------
# Новый <script> блок — полная реализация экранов 1–4.
# ---------------------------------------------------------------------------

SCRIPT_REPLACE_FROM = "<script>\n/* ================= МОК-ДАННЫЕ ================= */"
SCRIPT_REPLACE_TO = "</script>"

NEW_SCRIPT = r"""<script>
/* =================================================================
 * Rozitta Librarian — UI Этап 1 (реализация UI-1 + UI-2 + UI-3 + UI-4)
 * Спецификация: /home/z/my-project/upload/librarian_ui_спецификация_этап1.md
 *
 * Подключение к бэкенду: WebSocket → ws://<host>/ws
 *   Операции: list_archives, scan_archives, open_archive,
 *             list_shelves, stats, whats_new, top_terms,
 *             search, get_message
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
  // UI-3: поиск
  query: '',
  searchResults: [],       // последний ответ search (hits[])
  searchFilters: {author: null, date_from: null, date_to: null},
  // UI-4: ридер
  readerMsg: null,         // последний ответ get_message
  readerCommentId: null,   // ?c= параметр — прокрутиться к этому комментарию
  backCtx: null,           // { label, restore() }
  rzShown: {},
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
    wsSend({op: 'list_archives'});
  };

  S.ws.onclose = () => {
    S.wsConnected = false;
    $('conn-warn').style.display = 'inline-flex';
    devLog('WebSocket: соединение закрыто', 'warn');
    setTimeout(() => { if (!S.wsConnected) wsConnect(); }, 2000);
  };

  S.ws.onerror = () => devLog('WebSocket: ошибка', 'error');

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
    S.archive = data.card;
    S.archiveFull = data.passport;
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
    if (S._pendingShelf){
      renderShelfFromWhatsNew(S._pendingShelf);
      S._pendingShelf = null;
    } else {
      renderStart();
    }
    return;
  }
  if (op === 'top_terms'){
    // Обновляем чипы, если они пришли отдельной операцией
    if (data.terms && S.archive){
      S.archive.chips = data.terms;
      renderSidebarAfterOpen();
    }
    return;
  }
  if (op === 'search'){
    S.query = data.query || S.query;
    S.searchResults = data.hits || [];
    S.searchFilters = data.filters || S.searchFilters;
    renderSearchResults();
    return;
  }
  if (op === 'get_message'){
    S.readerMsg = data;
    renderReader(data);
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
  // Enter в строке поиска — запуск поиска
  $('q-input').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
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
      openArchive(p[1], /*navigate=*/false);
      show('archive');
      return;
    }
    if (p[2] === 'shelf' && p[3])      renderShelf(p[3]);
    else if (p[2] === 'search')        {
      const q = new URLSearchParams(queryPart||'').get('q')||'';
      if (q){ $('q-input').value = q; runSearch(q); }
      else renderStart();
    }
    else if (p[2] === 'm' && p[3])     {
      const cmt = new URLSearchParams(queryPart||'').get('c');
      openReader(parseInt(p[3],10), cmt ? parseInt(cmt,10) : null);
    }
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

/* ---- Экран 1: список архивов ---- */
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

/* ---- Открытие архива ---- */
function openArchive(id, navigate=true){
  S.archive = null; S.shelves = []; S.statsData = null; S.whatsNew = [];
  S.searchResults = []; S.readerMsg = null;
  show('archive');
  wsSend({op: 'open_archive', archive_id: id});
  if (navigate) nav(`#/a/${id}`);
}

function renderSidebarAfterOpen(){
  if (!S.archive) return;
  const a = S.archive;
  $('side-emoji').textContent = a.emoji || '📦';
  $('side-name').textContent = a.title || a.id;

  let m = 0, r = 0;
  if (Array.isArray(S.shelves)){
    for (const sh of S.shelves){
      if (sh.kind === 'messages') m = sh.count || 0;
      if (sh.kind === 'transcriptions') r = sh.count || 0;
    }
  }
  if (m === 0 && a.messages_count) m = a.messages_count;
  if (r === 0 && a.transcriptions_count) r = a.transcriptions_count;

  $('cnt-messages').textContent = m;
  $('cnt-records').textContent = r;
  $('toc-records').style.display = r ? 'flex' : 'none';

  $('sum-handle').textContent = a.username || '—';
  $('sum-type').textContent = a.type_label || a.chat_type || '—';
  $('sum-dates').textContent = a.date_period || '—';
  $('sum-msg').textContent = m;
  $('sum-rec').textContent = r;

  // Чипы — теперь динамические, вычисляются бэкендом из FTS5
  const chips = a.chips || [];
  $('chips').innerHTML = chips.length
    ? chips.map(c => `<span class="chip" onclick="chipSearch('${esc(c).replace(/'/g,"\\'")}')">${esc(c)}</span>`).join(' ')
    : '<span style="color:var(--text-ter);font-size:0.72rem;">(архив только что открылся — попробуй любое слово)</span>';
}

function renderStart(){
  if (!S.archive) return;
  const a = S.archive;
  $('start-title').textContent = a.title;
  const m = a.messages_count || 0;
  const r = a.transcriptions_count || 0;
  $('start-sub').textContent = `${a.type_label || a.chat_type} · ${m} сообщений${r?` · ${r} запись`:''} · ${a.date_period || ''}`;

  const recent = S.whatsNew || [];
  if (recent.length){
    $('start-recent').innerHTML = recent.map(i => msgCardFromWhatsNew(i, 'start')).join('');
  } else {
    $('start-recent').innerHTML = `<div style="color:var(--text-ter);padding:20px 0;font-size:0.8rem;">Загружаю последние сообщения…</div>`;
  }
  showView('start');
  $('q-input').focus();
  rzSay('side','first-open','Начни с поиска или загляни на полку слева. Не знаешь, что искать — попробуй слова под строкой поиска.');
}

/* Карточка сообщения из ответа whats_new (для стартовой страницы и полок).
 * whats_new возвращает: { internal_id, chat_id, message_id, author, date, text_preview, url }
 */
function msgCardFromWhatsNew(item, from='start'){
  const id   = item.message_id || item.id || '?';
  const text = item.text_preview || item.text || '';
  const date = item.date || '';
  const author = item.author || item.username || '';
  const preview = text.length > 150 ? text.slice(0, 150) + '…' : text;
  return `<div class="msg-card" onclick="openReader(${id}, null, '${from}')" style="cursor:pointer;">
    <div class="mc-head">
      <span class="mc-id">#${esc(id)}</span>
      <span class="mc-date">${esc(formatDateShort(date))}</span>
      <span class="mc-shelf">💬 сообщение</span>
    </div>
    <div class="mc-text">${esc(preview) || '<em>(без текста)</em>'}</div>
    <div class="mc-read">Читать →</div>
  </div>`;
}

/* ---- Полка ----
 * whats_new сейчас не фильтрует по shelf. Для полки "messages" это ок.
 * Для полки "records" — будут показаны сообщения вперемешку. Когда добавим
 * фильтр по shelf в whats_new — заменим.
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
    ? items.map(i => msgCardFromWhatsNew(i, 'shelf')).join('')
    : '<div style="color:var(--text-ter);padding:20px 0;font-size:0.8rem;">На этой полке ничего нет.</div>';
  showView('shelf');
}

/* ================= UI-3: ПОИСК (РЕАЛЬНЫЕ ДАННЫЕ) ================= */

/* toggleAdv — раскрыть/свернуть «Точный поиск» */
function toggleAdv(){
  $('adv-panel').classList.toggle('open');
  $('adv-toggle').classList.toggle('open');
}

/* chipSearch — клик по чипу */
function chipSearch(q){
  $('q-input').value = q;
  doSearch();
}

/* doSearch — обработчик Enter / кнопки «Искать» */
function doSearch(){
  const q = $('q-input').value.trim();
  if (!q) return;
  nav(`#/a/${S.archive.id}/search?q=${encodeURIComponent(q)}`);
}

/* runSearch — посылает WS-операцию search с расширенными фильтрами */
function runSearch(q){
  if (!S.archive) return;
  // Собираем фильтры из adv-панели
  const author = $('q-author').value.trim() || null;
  const dateFrom = $('q-from').value || null;
  const dateTo = $('q-to').value || null;
  // Если дата есть — приводим к ISO (с временем), т.к. parser.db хранит даты с T...
  const args = {query: q};
  if (author) args.author = author;
  if (dateFrom) args.date_from = dateFrom + 'T00:00:00';
  if (dateTo)   args.date_to   = dateTo   + 'T23:59:59';
  S.query = q;
  S.searchResults = []; // очистим старые результаты
  wsSend({op: 'search', archive_id: S.archive.id, args});
  // Сразу покажем экран результатов с лоадером
  $('results-count').innerHTML = `<span style="color:var(--text-ter)">Ищу «${esc(q)}»…</span>`;
  $('results-list').innerHTML = '';
  showView('results');
}

/* renderSearchResults — отрисовка после ответа search */
function renderSearchResults(){
  const q = S.query;
  const hits = S.searchResults || [];
  const n = hits.length;
  if (!n){
    $('results-count').innerHTML = '';
    $('results-list').innerHTML = `<div class="no-results"><div class="big">🐸</div>
      По слову «${esc(q)}» я ничего не нашла.<br>
      Попробуй начало слова или другой корень — я ищу по началу слов.</div>`;
    rzSay('side','zero-results',`По слову «${q}» ничего не нашла. Попробуй начало слова или другой корень.`);
    return;
  }
  const w = n === 1 ? 'совпадение' : n < 5 ? 'совпадения' : 'совпадений';
  $('results-count').innerHTML = `Найдено: <b>${n}</b> ${w} по запросу «${esc(q)}»`;
  $('results-list').innerHTML = hits.map(h => searchHitCard(h)).join('');
  rzSay('side','first-hit','Нашла! Нажми «Читать», чтобы открыть сообщение целиком.');
}

/* searchHitCard — карточка одного результата.
 * hit = {
 *   message_id, post_message_id, is_comment, author, date, snippet, url,
 *   source, ...
 * }
 * Сниппет приходит с <<H>>..<</H>> — превращаем в <mark>..</mark>.
 */
function searchHitCard(h){
  const isComment = !!h.is_comment;
  const targetId = isComment && h.post_message_id ? h.post_message_id : h.message_id;
  const commentId = isComment ? h.message_id : null;
  const snippetHtml = highlightSnippet(h.snippet || '');
  const shelfLabel = h.source === 'transcription'
    ? '🎙 расшифровка'
    : isComment
      ? '↳ в комментарии'
      : '💬 сообщение';
  const shelfClass = h.source === 'transcription' ? 'rec' : (isComment ? 'com' : '');
  return `<div class="msg-card" onclick="openReader(${targetId}, ${commentId ? commentId : 'null'}, 'results')" style="cursor:pointer;">
    <div class="mc-head">
      <span class="mc-id">#${esc(targetId)}</span>
      <span class="mc-date">${esc(formatDateShort(h.date))}</span>
      <span class="mc-author">@${esc((h.author||'').replace('@',''))}</span>
      <span class="mc-shelf ${shelfClass}">${shelfLabel}</span>
    </div>
    <div class="mc-text">${snippetHtml}</div>
    <div class="mc-read">Читать →</div>
  </div>`;
}

/* highlightSnippet — превращает <<H>>..<</H>> в <mark>..</mark> */
function highlightSnippet(s){
  return esc(s||'')
    .replace(/&lt;&lt;H&gt;&gt;/g, '<mark>')
    .replace(/&lt;&lt;\/H&gt;&gt;/g, '</mark>');
}

/* ================= UI-4: РИДЕР (РЕАЛЬНЫЕ ДАННЫЕ) ================= */

/* openReader — вызывает WS-операцию get_message.
 * message_id — целевой пост (или родительский пост комментария).
 * commentId — необязательный ?c= параметр: к этому комментарию прокрутиться.
 * from — контекст возврата: 'start' | 'shelf' | 'results'.
 */
function openReader(messageId, commentId, from){
  // Запоминаем контекст возврата
  const view = document.querySelector('.view.active');
  const scroll = view ? view.scrollTop : 0;
  if (from === 'results'){
    const q = S.query, n = (S.searchResults||[]).length;
    S.backCtx = { label: `← К результатам («${q}», ${n})`,
      restore(){ nav(`#/a/${S.archive.id}/search?q=${encodeURIComponent(q)}`); setTimeout(()=> $('view-results').scrollTop = scroll, 0); } };
  } else if (from === 'shelf'){
    const sh = S.shelf;
    S.backCtx = { label: '← К полке',
      restore(){ nav(`#/a/${S.archive.id}/shelf/${sh}`); setTimeout(()=> $('view-shelf').scrollTop = scroll, 0); } };
  } else {
    S.backCtx = { label: '← К обзору', restore(){ nav(`#/a/${S.archive.id}`); } };
  }
  S.readerCommentId = commentId;
  S.readerMsg = null;

  // Сразу обновим URL (через nav, чтобы сработал route → а если уже там — вручную запросим)
  const hash = `#/a/${S.archive.id}/m/${messageId}` + (commentId ? `?c=${commentId}` : '');
  if (location.hash !== hash){
    nav(hash);
  }
  // Загружаем сообщение
  wsSend({op: 'get_message', archive_id: S.archive.id, args: {message_id: messageId}});
  // Лоадер
  $('rd-title').textContent = 'Загружаю…';
  $('rd-author').textContent = '—';
  $('rd-date').textContent = '—';
  $('rd-body').innerHTML = `<p style="color:var(--text-ter);">Открываю сообщение #${messageId}…</p>`;
  $('rd-tg').style.display = 'none';
  $('rd-recplate').style.display = 'none';
  $('rd-linked').innerHTML = '';
  $('rd-comments').innerHTML = '';
  $('rn-prev').disabled = true; $('rn-next').disabled = true;
  $('rn-prev-t').textContent = ''; $('rn-next-t').textContent = '';
  $('reader-back').textContent = S.backCtx ? S.backCtx.label : '← Назад';
  showView('reader');
}

/* renderReader — отрисовка ридера после ответа get_message.
 * data = {
 *   post: { message_id, author, username, date, text, is_comment, post_id, ... },
 *   is_voice: bool,
 *   transcription: { text, model_type, created_at } | null,
 *   comments: { total, items: [{ message_id, author, username, date, text, ... }] },
 *   neighbors: { prev: { message_id, date } | null, next: { ... } | null },
 *   telegram_link: "https://t.me/..." | null,
 * }
 */
function renderReader(data){
  if (!data || !data.post){
    $('rd-title').textContent = 'Сообщение не найдено';
    $('rd-body').innerHTML = `<p style="color:var(--text-sec);">Не удалось загрузить сообщение.</p>`;
    return;
  }
  const p = data.post;
  const isVoice = !!data.is_voice;
  const tr = data.transcription;
  const coms = (data.comments && data.comments.items) || [];
  const cmtTotal = (data.comments && data.comments.total) || 0;
  const nb = data.neighbors || {};
  const tg = data.telegram_link;

  // Подсветка поискового запроса — если пришли из поиска
  const tokens = (S.backCtx && S.query && S.backCtx.label && S.backCtx.label.includes('результатам'))
    ? tokenizeQuery(S.query) : [];

  $('reader-back').textContent = S.backCtx ? S.backCtx.label : '← К обзору';
  $('rd-title').textContent = (isVoice ? 'Запись' : 'Сообщение') + ' #' + p.message_id;
  $('rd-recplate').style.display = isVoice ? 'inline-flex' : 'none';
  $('rd-ava').textContent = (p.author||'@?').replace('@','')[0].toUpperCase();
  $('rd-author').textContent = p.author || '—';
  $('rd-date').textContent = formatDateFull(p.date);

  // Текст поста. Для голосового — показываем текст поста + блок расшифровки.
  let bodyHtml = renderTextWithHighlight(p.text, tokens);
  if (tr){
    bodyHtml += `<div class="rd-transcription">
      <div class="rd-tr-h">🎙 Расшифровка аудио${tr.model_type ? ` <span class="rd-tr-model">(${esc(tr.model_type)})</span>` : ''}</div>
      <div class="rd-tr-text">${renderTextWithHighlight(tr.text, tokens)}</div>
    </div>`;
  }
  $('rd-body').innerHTML = bodyHtml;

  // Telegram-ссылка
  if (tg){
    $('rd-tg').href = tg;
    $('rd-tg').style.display = 'inline-flex';
  } else {
    $('rd-tg').style.display = 'none';
  }
  $('rd-linked').innerHTML = '';

  // Комментарии
  if (coms.length){
    let html = `<div class="rd-com-h">💬 Комментарии (${cmtTotal})</div>`;
    html += coms.map(c => {
      const isTarget = S.readerCommentId && c.message_id === S.readerCommentId;
      const cls = isTarget ? 'comment-row target' : 'comment-row';
      return `<div class="${cls}" data-cid="${c.message_id}">
        <span class="c-author">${esc(c.author||'анон')}</span>
        <span class="c-date">${esc(formatDateFull(c.date))}</span>
        <div class="c-text">${renderTextWithHighlight(c.text, tokens)}</div>
      </div>`;
    }).join('');
    $('rd-comments').innerHTML = html;
  } else {
    $('rd-comments').innerHTML = '';
  }

  // Соседи
  const prev = nb.prev, next = nb.next;
  $('rn-prev').disabled = !prev;
  $('rn-next').disabled = !next;
  $('rn-prev-t').textContent = prev ? `#${prev.message_id} · ${formatDateShort(prev.date)}` : '';
  $('rn-next-t').textContent = next ? `#${next.message_id} · ${formatDateShort(next.date)}` : '';
  $('rn-prev').dataset.id = prev ? prev.message_id : '';
  $('rn-next').dataset.id = next ? next.message_id : '';

  showView('reader');
  $('view-reader').scrollTop = 0;

  // Прокрутка к целевому комментарию (если пришли из поиска по комментарию)
  if (S.readerCommentId){
    setTimeout(() => {
      const el = document.querySelector(`.comment-row[data-cid="${S.readerCommentId}"]`);
      if (el){
        el.scrollIntoView({behavior:'smooth', block:'center'});
        el.classList.add('flash');
      } else {
        // Комментрий не найден — прокрутим к первому <mark>
        const m = $('rd-body').querySelector('mark') || $('rd-comments').querySelector('mark');
        if (m) m.scrollIntoView({block:'center'});
      }
    }, 100);
  } else {
    // Прокрутка к первому подсвеченному фрагменту (если пришли из поиска)
    const firstMark = $('rd-body').querySelector('mark') || $('rd-comments').querySelector('mark');
    if (firstMark) firstMark.scrollIntoView({block:'center'});
  }
}

function rdNav(dir){
  const id = $(dir<0?'rn-prev':'rn-next').dataset.id;
  if (id) openReader(parseInt(id,10), null, S.backCtx && S.backCtx.label && S.backCtx.label.includes('результатам') ? 'results' : 'shelf');
}
function goBack(){
  if (S.backCtx) S.backCtx.restore();
  else if (S.archive) nav(`#/a/${S.archive.id}`);
  else nav('#/');
}

/* ================= ВСПОМОГАТЕЛЬНЫЕ ================= */

/* tokenizeQuery — разбивает запрос на токены для подсветки.
 * Учитываем, что FTS5 работает по префиксам: «обесценива» подсветит
 * и «обесценивание», и «обесценивающее». Подсвечиваем по началу слова.
 */
function tokenizeQuery(q){
  return (q||'').toLowerCase().split(/\s+/).filter(t => t.length >= 2);
}
function renderTextWithHighlight(text, tokens){
  const safe = esc(text||'');
  if (!tokens || !tokens.length) return safe;
  // Подсветка: ищем начала слов, начинающиеся с токена.
  // Берём оригинальный текст, экранируем, потом применяем regex по позициям слов.
  // Простой подход: regex с boundary-подобной группой.
  try {
    const re = new RegExp(
      '(^|[^а-яa-zё0-9])(' +
      tokens.map(t => t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|') +
      ')([а-яa-zё0-9]*)',
      'gi'
    );
    return safe.replace(re, (m,pre,tok,rest) => pre + '<mark>' + tok + rest + '</mark>');
  } catch {
    return safe;
  }
}

/* formatDateShort — '2024-09-15T10:30:00' → '15 сен 2024, 10:30' */
const _RU_MONTHS_SHORT = ['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
function formatDateShort(iso){
  if (!iso) return '—';
  try {
    const d = new Date(iso);
    if (isNaN(d)) return iso.slice(0,16).replace('T',' ');
    const day = d.getDate();
    const mon = _RU_MONTHS_SHORT[d.getMonth()];
    const yr  = d.getFullYear();
    const hh  = String(d.getHours()).padStart(2,'0');
    const mm  = String(d.getMinutes()).padStart(2,'0');
    return `${day} ${mon} ${yr}, ${hh}:${mm}`;
  } catch { return iso; }
}
function formatDateFull(iso){
  return formatDateShort(iso);
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
</script>"""


def build():
    src = SRC_MOCK.read_text(encoding="utf-8")
    start_marker = "<script>\n/* ================= МОК-ДАННЫЕ ================= */"
    end_marker = "</script>"

    start = src.find(start_marker)
    if start < 0:
        raise SystemExit("Не нашёл start_marker в макете — макет изменился, обновите скрипт")
    end = src.find(end_marker, start)
    if end < 0:
        raise SystemExit("Не нашёл </script> после start_marker")

    new_html = src[:start] + NEW_SCRIPT + src[end + len(end_marker):]

    new_html = new_html.replace(
        "<title>Rozitta Librarian — макет UI (Этап 1)</title>",
        "<title>Rozitta Librarian — Читальный зал</title>",
    )
    new_html = new_html.replace(
        '<span class="acc">макет UI — данные демонстрационные</span>',
        '<span class="acc">этап 1 · экраны 1–4 — реальные данные через WebSocket</span>',
    )

    DST.write_text(new_html, encoding="utf-8")
    print(f"OK: {DST} ({len(new_html)} байт)")


if __name__ == "__main__":
    build()
