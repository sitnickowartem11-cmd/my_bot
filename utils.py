# app/utils.py
# Вспомогательные функции для бота

import random
import string
import re


def generate_game_id(length: int = 8) -> str:
    """Генерирует код игры: 8 символов A-Z + 0-9."""
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def format_display_name(username: str | None, full_name: str | None, user_id: int) -> str:
    """Возвращает красивое отображаемое имя."""
    if username:
        return username
    if full_name:
        return full_name
    return str(user_id)


def username_is_valid_for_link(username: str | None) -> bool:
    """Проверяет, можно ли сделать кликабельную ссылку на username."""
    if not username:
        return False
    return bool(re.match(r'^[A-Za-z0-9_]{5,32}$', username))
