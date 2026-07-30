"""
core/groups.py — детерминированные группы источников (Г1+Г2).

Группа — это ПРЕДСТАВЛЕНИЕ поверх существующих полей parser.db и паспорта,
а не копия данных. Группа НЕ хранит текст, только принадлежность сообщений.

Поддерживаемые типы (Г2):

  post_thread          — все сообщения под одним постом (пост + все его
                         комментарии). Ключ: post_message_id.
  participant_thread   — под-нить одного участника под постом: сообщения
                         этого участника + прямые ответы автора на них
                         (через reply_to_msg_id). Ключ: post_message_id +
                         username участника.
  cycle                — полка из паспорта архива (kind из shelves).
                         Ключ: shelf.kind.
  transcript           — пост с расшифровкой голосового сообщения. Ключ:
                         post_message_id (где есть transcription в parser.db
                         ИЛИ в 00_Индекс.md).
  month                — все посты одного месяца. Ключ: YYYY-MM.

Не реализовано в этом патче:
  book_section         — нарезка книги по markdown-заголовкам (##/###).
                         Зависит от патча 7 (text_folder source).

Правила (из рекомендации Г1):
- Ни одной копии текста сообщений в librarian.db.
- Ни одного LLM-вызова.
- Группы вычисляются детерминированно — один и тот же parser.db всегда даёт
  один и тот же набор групп.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .archive import Archive
from .parser_db import ParserDB


# Типы групп
GROUP_TYPE_POST_THREAD = "post_thread"
GROUP_TYPE_PARTICIPANT_THREAD = "participant_thread"
GROUP_TYPE_CYCLE = "cycle"
GROUP_TYPE_TRANSCRIPT = "transcript"
GROUP_TYPE_MONTH = "month"

ALL_GROUP_TYPES = (
    GROUP_TYPE_POST_THREAD,
    GROUP_TYPE_PARTICIPANT_THREAD,
    GROUP_TYPE_CYCLE,
    GROUP_TYPE_TRANSCRIPT,
    GROUP_TYPE_MONTH,
)


@dataclass
class Group:
    """Группа источников. Не хранит текст, только принадлежность."""

    # Стабильный идентификатор, например:
    #   "post_thread:400"
    #   "participant_thread:400:@marina_s"
    #   "cycle:messages"
    #   "transcript:850"
    #   "month:2024-09"
    group_id: str

    # Тип группы — одна из GROUP_TYPE_*
    type: str

    # Человекочитаемое название для UI:
    #   "Пост 400 + 3 комментария"
    #   "Ветка @marina_s под постом 400 (4 сообщения)"
    #   "Сообщения" (полка)
    #   "Расшифровка поста 850"
    #   "Сентябрь 2024"
    label: str

    # Структурированные ключи группы — для поиска и группировки:
    #   {"post_message_id": 400}
    #   {"post_message_id": 400, "username": "@marina_s"}
    #   {"shelf_kind": "messages"}
    #   {"post_message_id": 850}
    #   {"month": "2024-09"}
    keys: dict = field(default_factory=dict)

    # message_id всех сообщений, входящих в группу (упорядочены по дате)
    message_ids: list[int] = field(default_factory=list)

    # chat_id (один на архив; для удобства фильтрации)
    chat_id: Optional[int] = None

    # Дополнительные поля (количество, период, автор и т.д.)
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Сериализация для API/UI.

        Поле `citation` — Г6: готовая строка-ссылка на группу, если для
        неё осмысленно сослаться (post_thread, participant_thread,
        transcript). Для cycle и month — None (это мета-группы, на них
        нельзя сослаться как на источник).
        """
        # Локальный импорт — чтобы не было цикла (citations → groups).
        try:
            from .citations import citation_for_group, format_citation
            cit = citation_for_group(self)
            citation_text = format_citation(cit) if cit else None
        except Exception:
            citation_text = None
        return {
            "group_id": self.group_id,
            "type": self.type,
            "label": self.label,
            "keys": self.keys,
            "message_ids": self.message_ids,
            "chat_id": self.chat_id,
            "count": len(self.message_ids),
            "extras": self.extras,
            "citation": citation_text,
        }

# ---------------------------------------------------------------------------
# Г3: Групповое ранжирование — DTO и алгоритм
# ---------------------------------------------------------------------------

@dataclass
class GroupHit:
    """Группа с информацией о попаданиях поиска (Г3).

    Группа = представление, hit = точка входа. GroupHit — их пересечение:
    для каждой группы, в которую попал хотя бы один совпавший message_id,
    мы храним сколько совпало, лучший ранг среди совпавших и список
    matched_message_ids.
    """

    # Из Group (без message_ids — они избыточны в выдаче, оставляем только
    # matched_message_ids).
    group_id: str
    type: str
    label: str
    keys: dict = field(default_factory=dict)
    chat_id: Optional[int] = None
    extras: dict = field(default_factory=dict)

    # Сколько сообщений группы совпало (0 < matched_count ≤ total_count)
    matched_count: int = 0
    # Всего сообщений в группе
    total_count: int = 0
    # Лучший ранг среди совпавших hits (0 = вершина поисковой выдачи)
    best_rank: int = 0
    # message_id лучшего совпавшего хита (для ссылки «перейти к лучшему»)
    best_message_id: Optional[int] = None
    # Все совпавшие message_id (отсортированы по рангу в выдаче)
    matched_message_ids: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """Сериализация для API/UI.

        Включает `ratio` — matched_count / total_count — для UI-прогресс-бара
        или подписи «3 из 9». Поле `citation` — Г6: готовая строка-ссылка
        на группу (для post_thread, participant_thread, transcript; None
        для cycle/month).
        """
        ratio = (self.matched_count / self.total_count) if self.total_count else 0.0
        # citation — по той же логике, что и у Group.
        try:
            from .citations import citation_for_group
            from .groups import Group
            # Временно собираем Group, чтобы переиспользовать citation_for_group.
            tmp_group = Group(
                group_id=self.group_id,
                type=self.type,
                label=self.label,
                keys=self.keys,
                message_ids=[],  # для citation не нужно
                chat_id=self.chat_id,
                extras=self.extras,
            )
            cit = citation_for_group(tmp_group)
            from .citations import format_citation
            citation_text = format_citation(cit) if cit else None
        except Exception:
            citation_text = None
        return {
            "group_id": self.group_id,
            "type": self.type,
            "label": self.label,
            "keys": self.keys,
            "chat_id": self.chat_id,
            "extras": self.extras,
            "matched_count": self.matched_count,
            "total_count": self.total_count,
            "best_rank": self.best_rank,
            "best_message_id": self.best_message_id,
            "matched_message_ids": list(self.matched_message_ids),
            "ratio": round(ratio, 3),
            "citation": citation_text,
        }


def rank_groups(hits, groups) -> list:
    """Агрегировать попадания поиска по группам (Г3).

    Алгоритм:
      1. Для каждой группы находим пересечение её message_ids с message_id
         всех hits. Пустое пересечение → группа не попадает в выдачу.
      2. Сортировка:
         a) matched_count DESC (больше совпадений = выше)
         b) best_rank ASC (раньше в hits = выше)
         c) total_count DESC (при равенстве — крупнее группа = выше)
      3. Возвращает только группы с matched_count >= 1.

    Один message_id может входить в несколько групп (например, пост 900
    входит в post_thread:900, cycle:messages, month:2024-10). Это
    нормально — группа показывает разные срезы одного материала.

    Параметры:
      hits:   list[SearchHit] — результат lib_db.search(...)
      groups: list[Group]     — результат GroupsBuilder.build_all()

    Возвращает: list[GroupHit], отсортированный по spec'у Г3.

    NB: один message_id может появиться в hits несколько раз (например,
    один раз как source="message", другой — как source="transcription"
    для того же поста). Берём минимальный ранг (т.е. лучшую позицию).
    """
    if not hits or not groups:
        return []

    # message_id → лучший (минимальный) ранг в выдаче.
    # NB: один message_id может появиться в hits несколько раз (например,
    # один раз как source="message", другой — как source="transcription"
    # для того же поста). Берём минимальный ранг (т.е. лучшую позицию).
    msg_to_rank: dict[int, int] = {}
    for rank, hit in enumerate(hits):
        mid = getattr(hit, "message_id", None)
        if mid is None:
            continue
        mid = int(mid)
        if mid not in msg_to_rank or rank < msg_to_rank[mid]:
            msg_to_rank[mid] = rank

    group_hits: list[GroupHit] = []
    for g in groups:
        if not g.message_ids:
            continue
        matched: list[int] = []
        best_rank: Optional[int] = None
        best_mid: Optional[int] = None
        for mid in g.message_ids:
            try:
                mid_i = int(mid)
            except (TypeError, ValueError):
                continue
            if mid_i in msg_to_rank:
                r = msg_to_rank[mid_i]
                matched.append(mid_i)
                if best_rank is None or r < best_rank:
                    best_rank = r
                    best_mid = mid_i
        if not matched or best_rank is None or best_mid is None:
            continue
        # Сортируем matched по рангу в выдаче (от лучшего к худшему).
        matched.sort(key=lambda m: msg_to_rank[m])
        group_hits.append(GroupHit(
            group_id=g.group_id,
            type=g.type,
            label=g.label,
            keys=dict(g.keys) if g.keys else {},
            chat_id=g.chat_id,
            extras=dict(g.extras) if g.extras else {},
            matched_count=len(matched),
            total_count=len(g.message_ids),
            best_rank=best_rank,
            best_message_id=best_mid,
            matched_message_ids=matched,
        ))

    # Сортировка по spec'у Г3:
    #   matched_count DESC, best_rank ASC, total_count DESC
    group_hits.sort(key=lambda gh: (-gh.matched_count, gh.best_rank, -gh.total_count))
    return group_hits




# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class GroupsBuilder:
    """
    Детерминированное построение групп из parser.db + паспорта.

    Конструктор принимает уже открытые Archive + ParserDB. LibrarianDB НЕ
    нужен — все ключи (post_message_id, is_comment, reply_to_msg_id,
    post_id) уже есть в parser.db. Для transcript-групп используется
    parser.db.transcriptions + 00_Индекс.md (как и для FTS-индекса).
    """

    def __init__(self, archive: Archive, parser_db: ParserDB):
        self.archive = archive
        self.parser_db = parser_db
        self._chat_id = archive.passport.chat_id

    # ------------------------------------------------------------------
    # Главные методы
    # ------------------------------------------------------------------

    def build_all(self) -> list[Group]:
        """Все группы всех типов."""
        groups: list[Group] = []
        groups.extend(self.post_threads())
        groups.extend(self.participant_threads())
        groups.extend(self.cycles())
        groups.extend(self.transcripts())
        groups.extend(self.months())
        return groups

    def by_type(self, group_type: str) -> list[Group]:
        """Группы одного типа. Если тип неизвестен — ValueError."""
        if group_type not in ALL_GROUP_TYPES:
            raise ValueError(
                f"Неизвестный тип группы: {group_type!r}. "
                f"Допустимые: {', '.join(ALL_GROUP_TYPES)}"
            )
        return {
            GROUP_TYPE_POST_THREAD: self.post_threads,
            GROUP_TYPE_PARTICIPANT_THREAD: self.participant_threads,
            GROUP_TYPE_CYCLE: self.cycles,
            GROUP_TYPE_TRANSCRIPT: self.transcripts,
            GROUP_TYPE_MONTH: self.months,
        }[group_type]()

    def for_message(self, message_id: int,
                    chat_id: Optional[int] = None) -> list[Group]:
        """Все группы, в которые входит данное сообщение.

        Используется в UI-ридере: показать «это сообщение входит в такие-то
        группы», и в групповом ранжировании (патч 5).
        """
        # Найдём запись сообщения в parser.db, чтобы узнать его post_id,
        # username, date и reply_to_msg_id.
        msg = self.parser_db.get_message_by_message_id_only(message_id)
        if msg is None:
            return []
        result: list[Group] = []
        # 1) post_thread: всегда (если у сообщения есть post_id ИЛИ оно
        #    само пост — post_id IS NULL).
        if msg.post_id is not None:
            # Это комментарий — группа по post_id
            result.extend(
                g for g in self.post_threads()
                if g.keys.get("post_message_id") == msg.post_id
            )
        else:
            # Это сам пост — тоже группа
            result.extend(
                g for g in self.post_threads()
                if g.keys.get("post_message_id") == msg.message_id
            )
        # 2) participant_thread: если это комментарий под постом от
        #    конкретного username — ищем группу (post, username).
        if msg.is_comment and msg.post_id is not None and msg.username:
            uname = msg.username if msg.username.startswith("@") else f"@{msg.username}"
            # Заметка: participant_thread для автора поста не создаётся
            # (автор не «участница обсуждения» в смысле Г2). Если это
            # сообщение автора под своим постом — participant_thread для
            # автора не найдётся.
            for g in self.participant_threads():
                if (g.keys.get("post_message_id") == msg.post_id and
                        g.keys.get("username") == uname):
                    result.append(g)
                    break
        # 3) cycle: ВСЕ группы cycle, в которые входит сообщение.
        # Полки — это классы по shelf.kind, message_id входит в них по
        # типу: 'messages' — все сообщения; 'transcriptions' — сообщения
        # с расшифровкой. Это дешёвая операция (полок обычно 1-3).
        for g in self.cycles():
            if message_id in g.message_ids:
                result.append(g)
        # 4) transcript: если у этого сообщения есть расшифровка.
        for g in self.transcripts():
            if g.keys.get("post_message_id") == message_id:
                result.append(g)
                break
        # 5) month: по дате сообщения.
        if msg.date:
            ym = msg.date[:7]  # YYYY-MM
            for g in self.months():
                if g.keys.get("month") == ym and message_id in g.message_ids:
                    result.append(g)
                    break
        return result

    # ------------------------------------------------------------------
    # post_thread
    # ------------------------------------------------------------------

    def post_threads(self) -> list[Group]:
        """Группы по постам: каждый пост + все его комментарии.

        Ключ: post_message_id (message_id самого поста).
        В message_ids — сначала пост, потом комментарии по дате.
        """
        groups: list[Group] = []
        # Все посты (is_comment=0), отсортированные по дате
        with self.parser_db.cursor() as cur:
            cur.execute(
                "SELECT message_id, date, username, text "
                "FROM messages WHERE is_comment = 0 "
                "ORDER BY date ASC, message_id ASC"
            )
            posts = cur.fetchall()
        for row in posts:
            post_id = int(row["message_id"])
            # Комментарии под этим постом (post_id = наш message_id)
            with self.parser_db.cursor() as cur:
                cur.execute(
                    "SELECT message_id FROM messages "
                    "WHERE post_id = ? AND is_comment = 1 "
                    "ORDER BY date ASC, message_id ASC",
                    (post_id,),
                )
                comment_ids = [int(r["message_id"]) for r in cur.fetchall()]
            all_ids = [post_id] + comment_ids
            n_comments = len(comment_ids)
            # Заголовок: первые 50 символов текста поста
            text_preview = (row["text"] or "").strip()[:60]
            if text_preview:
                label = f"Пост {post_id}"
                if text_preview:
                    label += f": {text_preview}"
                if n_comments:
                    label += f" (+{n_comments} комм.)"
            else:
                label = f"Пост {post_id}"
                if n_comments:
                    label += f" (+{n_comments} комм.)"
            groups.append(Group(
                group_id=f"post_thread:{post_id}",
                type=GROUP_TYPE_POST_THREAD,
                label=label,
                keys={"post_message_id": post_id},
                message_ids=all_ids,
                chat_id=self._chat_id,
                extras={
                    "post_message_id": post_id,
                    "comments_count": n_comments,
                    "total_count": len(all_ids),
                    "date": row["date"],
                },
            ))
        return groups

    # ------------------------------------------------------------------
    # participant_thread
    # ------------------------------------------------------------------

    def participant_threads(self) -> list[Group]:
        """Под-нити участниц: сообщения одного участника под постом +
        прямые ответы автора на них (через reply_to_msg_id).

        Алгоритм:
          1. Для каждого поста P собираем все комментарии под P.
          2. Группируем их по username (только не-авторы — автор поста
             не считается «участницей обсуждения» в смысле Г2).
          3. Для каждой пары (P, username) собираем:
             - все сообщения этого username под P,
             - все сообщения автора поста, у которых reply_to_msg_id
               указывает на одно из сообщений этого username.
          4. Группа существует только если участников ≥ 2 (иначе это
             одиночная реплика, не ветка).
        """
        groups: list[Group] = []
        # Кэш постов и их авторов
        with self.parser_db.cursor() as cur:
            cur.execute(
                "SELECT message_id, username, chat_id "
                "FROM messages WHERE is_comment = 0 ORDER BY date"
            )
            posts = cur.fetchall()
        for prow in posts:
            post_id = int(prow["message_id"])
            post_username = prow["username"] or ""
            if post_username and not post_username.startswith("@"):
                post_username = f"@{post_username}"
            chat_id = int(prow["chat_id"]) if prow["chat_id"] is not None else self._chat_id
            # Все комментарии под этим постом
            with self.parser_db.cursor() as cur:
                cur.execute(
                    "SELECT message_id, username, reply_to_msg_id, date "
                    "FROM messages WHERE post_id = ? AND is_comment = 1 "
                    "ORDER BY date ASC, message_id ASC",
                    (post_id,),
                )
                comments = cur.fetchall()
            if not comments:
                continue
            # Группируем по username (только не-автор поста)
            by_user: dict[str, list[dict]] = {}
            for c in comments:
                uname = c["username"] or "anon"
                if uname and not uname.startswith("@"):
                    uname = f"@{uname}"
                if uname == post_username:
                    # автора поста не считаем участницей
                    continue
                by_user.setdefault(uname, []).append({
                    "message_id": int(c["message_id"]),
                    "reply_to_msg_id": (
                        int(c["reply_to_msg_id"]) if c["reply_to_msg_id"] is not None else None
                    ),
                    "date": c["date"],
                })
            # Для каждой участницы — собираем её сообщения + ответы автора
            for uname, user_msgs in by_user.items():
                user_msg_ids = {m["message_id"] for m in user_msgs}
                # Ответы автора на сообщения этой участницы
                author_replies = []
                for c in comments:
                    c_uname = c["username"] or "anon"
                    if c_uname and not c_uname.startswith("@"):
                        c_uname = f"@{c_uname}"
                    if c_uname != post_username:
                        continue
                    reply_to = (int(c["reply_to_msg_id"])
                                if c["reply_to_msg_id"] is not None else None)
                    if reply_to in user_msg_ids:
                        author_replies.append({
                            "message_id": int(c["message_id"]),
                            "reply_to_msg_id": reply_to,
                            "date": c["date"],
                        })
                # Объединяем и сортируем по дате
                combined = user_msgs + author_replies
                combined.sort(key=lambda m: (m["date"], m["message_id"]))
                # Условие: ≥ 2 сообщений (иначе не ветка, а одиночная реплика)
                if len(combined) < 2:
                    continue
                all_ids = [m["message_id"] for m in combined]
                groups.append(Group(
                    group_id=f"participant_thread:{post_id}:{uname}",
                    type=GROUP_TYPE_PARTICIPANT_THREAD,
                    label=f"Ветка {uname} под постом {post_id} "
                          f"({len(all_ids)} сообщ.)",
                    keys={
                        "post_message_id": post_id,
                        "username": uname,
                    },
                    message_ids=all_ids,
                    chat_id=chat_id,
                    extras={
                        "post_message_id": post_id,
                        "username": uname,
                        "total_count": len(all_ids),
                        "participant_messages": len(user_msgs),
                        "author_replies": len(author_replies),
                        "date_from": combined[0]["date"],
                        "date_to": combined[-1]["date"],
                    },
                ))
        return groups

    # ------------------------------------------------------------------
    # cycle
    # ------------------------------------------------------------------

    def cycles(self) -> list[Group]:
        """Полки из паспорта архива (kind из archive_passport.shelves).

        Полка 'messages' включает все message_id сообщений.
        Полка 'transcriptions' включает message_id всех постов с расшифровкой.
        """
        groups: list[Group] = []
        shelves = self.archive.passport.shelves or []
        if not shelves:
            # Если полок в паспорте нет — фабрика одна базовая 'messages'
            shelves = []
        # Полный список message_id всех сообщений (для messages-полки)
        with self.parser_db.cursor() as cur:
            cur.execute("SELECT message_id FROM messages ORDER BY date, message_id")
            all_msg_ids = [int(r["message_id"]) for r in cur.fetchall()]
        # Полный список message_id постов с расшифровкой
        transcript_post_ids = self._transcript_post_ids()
        for shelf in shelves:
            kind = shelf.kind
            if kind == "messages":
                msg_ids = all_msg_ids
            elif kind == "transcriptions":
                msg_ids = sorted(transcript_post_ids)
            else:
                # Неизвестная полка (media, reference_material и т.д.) —
                # включаем в группу все сообщения, точнее сказать не можем.
                # На практике такие полки не содержат message_id, поэтому
                # оставляем пустой список (но сама группа существует — это
                # маркер, что такая полка есть).
                msg_ids = []
            groups.append(Group(
                group_id=f"cycle:{kind}",
                type=GROUP_TYPE_CYCLE,
                label=shelf.label or kind,
                keys={"shelf_kind": kind},
                message_ids=msg_ids,
                chat_id=self._chat_id,
                extras={
                    "shelf_kind": kind,
                    "shelf_label": shelf.label,
                    "shelf_count": shelf.count,
                    "total_count": len(msg_ids),
                },
            ))
        return groups

    # ------------------------------------------------------------------
    # transcript
    # ------------------------------------------------------------------

    def transcripts(self) -> list[Group]:
        """Посты с расшифровкой голосовых сообщений.

        Источник расшифровок: parser.db.transcriptions (если есть) +
        файлы, перечисленные в 00_Индекс.md (как в Б3). Каждая группа
        содержит message_id одного поста (расшифрованного).
        """
        transcript_post_ids = self._transcript_post_ids()
        groups: list[Group] = []
        for post_id in sorted(transcript_post_ids):
            groups.append(Group(
                group_id=f"transcript:{post_id}",
                type=GROUP_TYPE_TRANSCRIPT,
                label=f"Расшифровка поста {post_id}",
                keys={"post_message_id": post_id},
                message_ids=[post_id],
                chat_id=self._chat_id,
                extras={
                    "post_message_id": post_id,
                    "total_count": 1,
                },
            ))
        return groups

    def _transcript_post_ids(self) -> set[int]:
        """Множество message_id постов, у которых есть расшифровка."""
        result: set[int] = set()
        # 1) Из parser.db.transcriptions
        with self.parser_db.cursor() as cur:
            cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='transcriptions'"
            )
            if cur.fetchone() is not None:
                cur.execute("SELECT DISTINCT message_id FROM transcriptions")
                for r in cur.fetchall():
                    try:
                        result.add(int(r["message_id"]))
                    except (TypeError, ValueError):
                        pass
        # 2) Из 00_Индекс.md (как в Б3)
        try:
            from .librarian_db import LibrarianDB
            file_rows = LibrarianDB._load_index_transcripts(
                self.parser_db.path.parent
            )
            for post_id, _name, _text in file_rows:
                result.add(int(post_id))
        except Exception:
            # 00_Индекс.md может не быть — это нормально
            pass
        return result

    # ------------------------------------------------------------------
    # month
    # ------------------------------------------------------------------

    def months(self) -> list[Group]:
        """Группы по году-месяцу: все посты одного месяца.

        Ключ: YYYY-MM (например, '2024-09').
        В message_ids — только посты (is_comment=0), не комментарии.
        Это сознательное решение: месяц — единица контента автора, а
        не討论ения.
        """
        groups: list[Group] = []
        # Группируем посты по YYYY-MM
        by_month: dict[str, list[int]] = {}
        with self.parser_db.cursor() as cur:
            cur.execute(
                "SELECT message_id, date FROM messages "
                "WHERE is_comment = 0 ORDER BY date, message_id"
            )
            for r in cur.fetchall():
                date = r["date"] or ""
                if len(date) < 7:
                    continue
                ym = date[:7]  # YYYY-MM
                by_month.setdefault(ym, []).append(int(r["message_id"]))
        # Человекочитаемые названия месяцев
        ru_months = [
            "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
            "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
        ]
        for ym in sorted(by_month.keys()):
            try:
                year, month = ym.split("-")
                month_idx = int(month)
                if 1 <= month_idx <= 12:
                    label = f"{ru_months[month_idx - 1]} {year}"
                else:
                    label = ym
            except (ValueError, IndexError):
                label = ym
            msg_ids = by_month[ym]
            groups.append(Group(
                group_id=f"month:{ym}",
                type=GROUP_TYPE_MONTH,
                label=f"{label} ({len(msg_ids)} пост.)",
                keys={"month": ym},
                message_ids=msg_ids,
                chat_id=self._chat_id,
                extras={
                    "month": ym,
                    "total_count": len(msg_ids),
                },
            ))
        return groups
