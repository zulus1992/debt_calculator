# -*- coding: utf-8 -*-
"""Кто есть кто в чате: состав участников, сопоставление имён и подсказки для ИИ.

Telegram присылает автора каждого сообщения (id, имя, @username) — из этих данных
постепенно собирается состав чата. По нему бот понимает, что «Лешак» из сообщения
«Лешак должен Диме 3» — это Леша Козлов (@kozlovAlex), и записывает долг на его user id.
Учёт идёт по пользователям, поэтому «Леша», «Лёха» и «Лешак» в одном чате — один человек.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

from storage import ChatMember

# Упрощённая морфология: снимаем до двух окончаний подряд («Дима»/«Диме» → «дим»).
CASE_ENDINGS = "аеёиоуыэюяйь"
# Слова, которые означают самого автора сообщения: «я должен Диме 3».
FIRST_PERSON = {"я", "меня", "мне", "мной", "мой", "моя", "мое", "себя", "себе"}


def normalize(value: str) -> str:
    """Сравнимый вид имени/ника: регистр ниже, без @, ё→е, без лишних символов."""
    text = str(value or "").strip().lstrip("@").lower().replace("ё", "е")
    text = re.sub(r"[^\w\s-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def name_key(value: str) -> str:
    """Ключ сравнения имён: «Диме»/«Дима» → «дим», «Дмитрий»/«Дмитрию» → «дмитр»."""
    key = normalize(value).replace(" ", "")
    for _ in range(2):                       # снимаем максимум два окончания подряд
        if len(key) >= 4 and key[-1] in CASE_ENDINGS:
            key = key[:-1]
        else:
            break
    return key


def member_from_telegram(chat_id: int, user: Mapping[str, object] | None) -> ChatMember | None:
    """Собирает участника из поля `from` апдейта Telegram (None — если это не человек)."""
    if not user:
        return None
    user_id = user.get("id")
    if user_id is None or user.get("is_bot"):
        return None
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    display = " ".join(part for part in (first, last) if part)
    return ChatMember(
        chat_id=int(chat_id),
        user_id=int(user_id),
        username=str(user.get("username") or "").lstrip("@"),
        display_name=display,
    )


def member_aliases(member: ChatMember) -> set[str]:
    """Все написания, по которым участника можно узнать в тексте."""
    parts = member.display_name.split()
    variants: set[str] = {member.username, member.display_name, *member.aliases}
    if parts:
        variants.add(parts[0])                          # только имя («Леша»)
        if len(parts) > 1:
            variants.add(" ".join(parts[:2]))           # имя + фамилия
            variants.add(parts[-1])                     # только фамилия
    return {normalize(variant) for variant in variants if str(variant).strip()}


def member_by_id(user_id: int | None, members: Iterable[ChatMember]) -> ChatMember | None:
    """Участник по user id (проверяем, что ИИ не выдумал id, которого нет в чате)."""
    if not user_id:
        return None
    for member in members:
        if member.user_id == int(user_id):
            return member
    return None


def _similar(left: str, right: str) -> bool:
    """Похожи ли написания: «Леша» ≈ «Лешак» (общий корень)."""
    if not left or not right or min(len(left), len(right)) < 4:
        return False
    return left.startswith(right) or right.startswith(left)


def resolve_member(value: str | None, members: Sequence[ChatMember],
                   author: ChatMember | None = None) -> ChatMember | None:
    """Ищет участника чата по имени/нику, как он написан в сообщении.

    Возвращает None, если совпадений нет или их несколько («Дима» при двух Дмитриях) —
    тогда решает ИИ по всему составу чата, а имя сохраняется как написано.
    """
    text = normalize(value)
    if not text:
        return None
    if text in FIRST_PERSON:
        return author
    handle = text.replace(" ", "")
    key = name_key(text)
    tokens = text.split()

    scored: list[tuple[float, ChatMember]] = []
    for member in members:
        aliases = member_aliases(member)
        keys = {name_key(alias) for alias in aliases}
        score = 0.0
        if member.username and normalize(member.username) == handle:
            score = 3.0                                  # явное «@kozlovAlex»
        elif text in aliases:
            score = 2.0                                  # точное совпадение
        elif key and key in keys:
            score = 1.0                                  # падеж: «Диме» → «Дима»
        elif any(_similar(alias, token) for alias in aliases for token in tokens):
            score = 0.5                                  # похожее написание: «Лешак» → «Леша»
        if score:
            scored.append((score, member))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0][0]
    winners = {member.user_id: member for score, member in scored if score == best}
    return next(iter(winners.values())) if len(winners) == 1 else None


def resolve_side(value: str | None, user_id: int | None, members: Sequence[ChatMember],
                 author: ChatMember | None = None) -> ChatMember | None:
    """Определяет участника для стороны записи: id от ИИ → имя из текста → автор («я»)."""
    return member_by_id(user_id, members) or resolve_member(value, members, author)


def label_for(user_id: int | None, name: str, members: Sequence[ChatMember]) -> str:
    """Человекочитаемое имя: «Леша Козлов (@kozlovAlex)», если участник узнан."""
    member = member_by_id(user_id, members)
    if member is not None:
        return member.label
    return str(name or "").strip()


def format_roster(members: Sequence[ChatMember], author: ChatMember | None = None,
                  limit: int = 50) -> str:
    """Состав чата текстом — подсказка для ИИ, чтобы он узнавал людей по именам и никам."""
    listed = list(members)[:limit]
    if not listed and author is None:
        return ""
    lines = ["Участники чата (сопоставляй имена из сообщения с ними):"]
    for member in listed:
        parts = [f"id={member.user_id}"]
        if member.display_name:
            parts.append(member.display_name)
        if member.username:
            parts.append(f"@{member.username}")
        if member.aliases:
            parts.append("алиасы: " + ", ".join(member.aliases))
        lines.append("- " + " | ".join(parts))
    if author is not None:
        lines.append(f"Автор сообщения: {author.label} (id={author.user_id})")
    return "\n".join(lines)
