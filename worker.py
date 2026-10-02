# app/worker.py
# Aiogram background worker: обрабатывает команды Telegram-бота (HTML + UX)
# Исправлена работа с сессиями SQLAlchemy, обработка callback'ов, логирование неудачных DM,
# и устранены DetachedInstanceError и проблемы с датой в fake Message.

import asyncio
import logging
import queue
import threading
from datetime import datetime as _dt
from typing import Optional

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app.manager import GameManager
from app.messages import MESSAGES
from app.utils import username_is_valid_for_link
from app.database import SessionLocal
from app.models import Game, Participant

logger = logging.getLogger(__name__)

# Очередь апдейтов, куда webhook кладёт данные
update_queue = queue.Queue()

# Пользователи, ожидающие ввода названия игры
pending_new_game = set()


def _safe_message_date_to_int(msg_date) -> int:
    """Преобразует поле date из message в int timestamp безопасно."""
    if msg_date is None:
        return int(_dt.utcnow().timestamp())
    if isinstance(msg_date, _dt):
        return int(msg_date.timestamp())
    try:
        return int(msg_date)
    except Exception:
        return int(_dt.utcnow().timestamp())


def start_worker(bot_token: str, bot_username: str):
    """Запускает aiogram worker в отдельном потоке."""

    def worker():
        bot = Bot(token=bot_token, parse_mode="HTML")
        dp = Dispatcher(bot, storage=MemoryStorage())

        # -------------------- Вспомогательные функции --------------------

        def main_menu_keyboard(is_admin: bool = False):
            kb = InlineKeyboardMarkup(row_width=2)
            kb.add(
                InlineKeyboardButton("🆕 Создать игру", callback_data="menu_newgame"),
                InlineKeyboardButton("🎯 Мои получатели", callback_data="menu_mytargets"),
            )
            kb.add(
                InlineKeyboardButton("📋 Мои игры", callback_data="menu_mygames"),
                InlineKeyboardButton("👥 Участники", callback_data="menu_players"),
            )
            kb.add(
                InlineKeyboardButton("ℹ️ Статус", callback_data="menu_status"),
                InlineKeyboardButton("❓ Помощь", callback_data="menu_help"),
            )
            if is_admin:
                kb.add(
                    InlineKeyboardButton("🎲 Запустить жеребьёвку", callback_data="menu_startgame"),
                    InlineKeyboardButton("🏁 Завершить игру", callback_data="menu_finishgame"),
                )
            return kb

        # -------------------- Команды --------------------

        @dp.message_handler(commands=['start'])
        async def cmd_start(message: types.Message):
            is_admin = False
            db = SessionLocal()
            try:
                is_admin = db.query(Game).filter(
                    Game.admin_id == message.from_user.id,
                    Game.is_active == True
                ).count() > 0
            finally:
                db.close()

            args = message.get_args()
            if args and args.startswith("join_"):
                code = args.replace("join_", "").upper()
                tg_username = (message.from_user.username or "").strip() or None
                full_name = " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])).strip() or tg_username or str(message.from_user.id)

                ok, res = GameManager.join_game(
                    code,
                    message.from_user.id,
                    tg_username,
                    full_name
                )
                if ok:
                    await bot.send_message(
                        message.chat.id,
                        MESSAGES["joined_game"],
                        reply_markup=main_menu_keyboard(is_admin=is_admin)
                    )
                else:
                    await bot.send_message(message.chat.id, res, reply_markup=main_menu_keyboard(is_admin=is_admin))
                return

            await bot.send_message(
                message.chat.id,
                MESSAGES["start_welcome"],
                reply_markup=main_menu_keyboard(is_admin=is_admin)
            )

        @dp.message_handler(commands=['help'])
        async def cmd_help(message: types.Message):
            await bot.send_message(message.chat.id, MESSAGES["help"])

        @dp.message_handler(commands=['newgame'])
        async def cmd_newgame(message: types.Message):
            uid = message.from_user.id
            pending_new_game.add(uid)
            await bot.send_message(message.chat.id, MESSAGES["newgame_prompt"])

        @dp.message_handler(commands=['join'])
        async def cmd_join(message: types.Message):
            parts = message.text.strip().split()
            if len(parts) < 2:
                await bot.send_message(message.chat.id, "❌ Укажите код: <b>/join ABC123XY</b>")
                return

            code = parts[1].upper()
            tg_username = (message.from_user.username or "").strip() or None
            full_name = " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])).strip() or tg_username or str(message.from_user.id)

            ok, res = GameManager.join_game(
                code,
                message.from_user.id,
                tg_username,
                full_name
            )
            await bot.send_message(message.chat.id, res)

        @dp.message_handler(commands=['startgame'])
        async def cmd_startgame(message: types.Message):
            db = SessionLocal()
            try:
                game = db.query(Game).filter(
                    Game.admin_id == message.from_user.id,
                    Game.is_started == False,
                    Game.is_active == True
                ).order_by(Game.created_at.desc()).first()

                if not game:
                    await bot.send_message(message.chat.id, "❌ У вас нет игр, которые можно запустить.")
                    return

                # Сохраняем простые значения, чтобы не обращаться к detached-объекту позже
                game_id = game.id
                game_name = game.name

                ok, res = GameManager.start_game(game_id, message.from_user.id)
                await bot.send_message(message.chat.id, res)

                if ok:
                    await bot.send_message(message.chat.id, MESSAGES["game_started"])

                    # Открываем новую сессию для рассылки — не используем старый объект game
                    db2 = SessionLocal()
                    failed = []
                    try:
                        participants = db2.query(Participant).filter(
                            Participant.game_id == game_id
                        ).all()

                        for p in participants:
                            # проверяем, что user_id — int
                            try:
                                uid = int(p.user_id)
                            except Exception:
                                logger.warning("Invalid user_id type for participant id=%s user_id=%s", getattr(p, "id", None), p.user_id)
                                failed.append((p.user_id, "invalid user_id"))
                                continue

                            if not p.target_id:
                                # если нет target_id — пропускаем
                                failed.append((uid, "no target assigned"))
                                continue

                            target = db2.query(Participant).filter(
                                Participant.game_id == game_id,
                                Participant.user_id == p.target_id
                            ).first()

                            if not target:
                                failed.append((uid, "target not found"))
                                continue

                            wishlist = target.wishlist or "Пожелания не указаны"
                            display = target.username or target.full_name or str(target.user_id)

                            try:
                                await bot.send_message(
                                    uid,
                                    MESSAGES["startgame_notify"].format(
                                        game_name=game_name,
                                        display=display,
                                        wishlist=wishlist
                                    )
                                )
                                logger.info("notify_sent: game=%s to=%s receiver=%s", game_id, uid, target.user_id)
                            except Exception as e:
                                logger.exception("Failed to send DM to %s: %s", uid, e)
                                failed.append((uid, str(e)))
                    finally:
                        db2.close()

                    # Если были неудачные отправки — уведомляем создателя в чате
                    if failed:
                        text_lines = ["<b>⚠️ Некоторым участникам не удалось отправить личные сообщения:</b>"]
                        for uid, reason in failed:
                            text_lines.append(f"- {uid}: {reason}")
                        await bot.send_message(message.chat.id, "\n".join(text_lines))

            finally:
                db.close()

        @dp.message_handler(commands=['finishgame'])
        async def cmd_finishgame(message: types.Message):
            db = SessionLocal()
            try:
                game = db.query(Game).filter(
                    Game.admin_id == message.from_user.id,
                    Game.is_active == True
                ).order_by(Game.created_at.desc()).first()

                if not game:
                    await bot.send_message(message.chat.id, "❌ У вас нет активных игр.")
                    return

                ok, res = GameManager.finish_game(game.id, message.from_user.id)
                await bot.send_message(message.chat.id, res)

                if ok:
                    participants = db.query(Participant).filter(
                        Participant.game_id == game.id
                    ).all()

                    for p in participants:
                        try:
                            await bot.send_message(
                                p.user_id,
                                MESSAGES["finishgame"].format(name=game.name)
                            )
                        except Exception:
                            pass

            finally:
                db.close()

        @dp.message_handler(commands=['wish'])
        async def cmd_wish(message: types.Message):
            text = message.text.strip()
            wishlist = text[6:].strip() if len(text) > 6 else ""

            if not wishlist:
                await bot.send_message(message.chat.id, "📝 Укажите пожелания: <b>/wish Хочу книгу</b>")
                return

            ok, res = GameManager.set_wishlist(message.from_user.id, wishlist)
            await bot.send_message(message.chat.id, res)

        @dp.message_handler(commands=['mytargets', 'mytarget'])
        async def cmd_mytargets(message: types.Message):
            results = GameManager.get_my_targets(message.from_user.id)

            if not results:
                await bot.send_message(message.chat.id, "📭 У вас пока нет активных назначений.")
                return

            lines = []
            for r in results:
                if not r.get("target_id"):
                    lines.append(
                        f"<b>Игра:</b> {r['game_name']} — получатель: ❌ не назначен"
                    )
                    continue

                display = r.get("target_username") or r.get("target_full_name") or str(r["target_id"])
                wishlist = r.get("target_wishlist") or "Пожелания не указаны"

                if username_is_valid_for_link(r.get("target_username")):
                    lines.append(
                        f"<b>Игра:</b> {r['game_name']}\n"
                        f"<b>Получатель:</b> <a href=\"https://t.me/{r['target_username']}\">{display}</a>\n"
                        f"<b>Пожелания:</b> {wishlist}"
                    )
                else:
                    lines.append(
                        f"<b>Игра:</b> {r['game_name']}\n"
                        f"<b>Получатель:</b> {display}\n"
                        f"<b>Пожелания:</b> {wishlist}"
                    )

            await bot.send_message(message.chat.id, "\n\n".join(lines))

        @dp.message_handler(commands=['mygames'])
        async def cmd_mygames(message: types.Message):
            db = SessionLocal()
            try:
                parts = db.query(Participant).filter(
                    Participant.user_id == message.from_user.id
                ).all()

                game_ids = {p.game_id for p in parts}

                if not game_ids:
                    await bot.send_message(message.chat.id, "📭 У вас пока нет игр.")
                    return

                lines = []
                for gid in game_ids:
                    g = db.query(Game).filter(Game.id == gid).first()
                    if not g:
                        continue

                    count = db.query(Participant).filter(
                        Participant.game_id == gid
                    ).count()

                    status = (
                        "Игра началась" if g.is_started else
                        ("Ожидание" if g.is_active else "Завершена")
                    )

                    lines.append(
                        f"• <b>{g.name}</b>\n"
                        f"  Код: <code>{g.id}</code>\n"
                        f"  Статус: {status}\n"
                        f"  Участников: {count}"
                    )

                await bot.send_message(message.chat.id, "<b>📋 Ваши игры:</b>\n\n" + "\n\n".join(lines))

            finally:
                db.close()

        @dp.message_handler(commands=['gameinfo'])
        async def cmd_gameinfo(message: types.Message):
            parts = message.text.strip().split()
            if len(parts) < 2:
                await bot.send_message(message.chat.id, "❌ Укажите код: <b>/gameinfo ABC123XY</b>")
                return

            code = parts[1].upper()
            info = GameManager.get_game_info(code)

            if not info:
                await bot.send_message(message.chat.id, f"❌ Игра с кодом <code>{code}</code> не найдена")
                return

            status_map = {
                "waiting": "Ожидание игроков",
                "active": "Игра началась",
                "finished": "Игра завершена"
            }

            extra = ""
            if info["participants"]:
                extra_lines = []
                for p in info["participants"]:
                    uname = p.get("username") or p.get("full_name") or str(p.get("user_id"))
                    mark = "📝" if p.get("has_wishlist") else "❔"

                    if username_is_valid_for_link(p.get("username")):
                        extra_lines.append(f"- <a href=\"https://t.me/{p.get('username')}\">{uname}</a> {mark}")
                    else:
                        extra_lines.append(f"- {uname} {mark}")

                extra = "\n".join(extra_lines)

            await bot.send_message(
                message.chat.id,
                MESSAGES["gameinfo"].format(
                    name=info["name"],
                    code=info["id"],
                    creator=info["creator_name"],
                    status=status_map.get(info["status"], info["status"]),
                    budget=info["budget"],
                    created=info["created_at"][:10] if info["created_at"] else "",
                    count=len(info["participants"]),
                    extra=extra,
                    bot=bot_username,
                )
            )

        @dp.message_handler(commands=['players'])
        async def cmd_players(message: types.Message):
            db = SessionLocal()
            try:
                p = db.query(Participant).filter(
                    Participant.user_id == message.from_user.id
                ).order_by(Participant.id.desc()).first()

                if not p:
                    await bot.send_message(message.chat.id, "❌ Вы не участвуете в игре.")
                    return

                g = db.query(Game).filter(Game.id == p.game_id).first()
                if not g:
                    await bot.send_message(message.chat.id, "❌ Игра не найдена.")
                    return

                participants = db.query(Participant).filter(
                    Participant.game_id == g.id
                ).all()

                lines = []
                for i, part in enumerate(participants, 1):
                    uname = part.username or part.full_name or str(part.user_id)
                    if username_is_valid_for_link(part.username):
                        link = f"<a href=\"https://t.me/{part.username}\">{uname}</a>"
                    else:
                        link = uname

                    creator_mark = " 👑" if part.user_id == g.admin_id else ""
                    wishlist_mark = " 📝" if part.wishlist else " ❔"

                    lines.append(f"{i}. {link}{creator_mark}{wishlist_mark}")

                await bot.send_message(
                    message.chat.id,
                    MESSAGES["participants_header"].format(name=g.name) + "\n" + "\n".join(lines)
                )

            finally:
                db.close()

        @dp.message_handler(commands=['status'])
        async def cmd_status(message: types.Message):
            db = SessionLocal()
            try:
                total_games = db.query(Game).count()
                active_games = db.query(Game).filter(Game.is_started == True).count()
                waiting_games = db.query(Game).filter(Game.is_started == False, Game.is_active == True).count()
                finished_games = db.query(Game).filter(Game.is_active == False).count()
                total_players = db.query(Participant).distinct(Participant.user_id).count()

                await bot.send_message(
                    message.chat.id,
                    MESSAGES["status"].format(
                        total=total_games,
                        active=active_games,
                        waiting=waiting_games,
                        finished=finished_games,
                        players=total_players,
                        queue=update_queue.qsize()
                    )
                )
            finally:
                db.close()

        # -------------------- Callback-кнопки меню --------------------

        @dp.callback_query_handler(lambda c: c.data and c.data.startswith("menu_"))
        async def menu_callbacks(callback_query: types.CallbackQuery):
            data = callback_query.data
            uid = callback_query.from_user.id
            chat_id = callback_query.message.chat.id
            msg_date_int = _safe_message_date_to_int(callback_query.message.date)

            if data == "menu_help":
                await bot.send_message(chat_id, MESSAGES["help"])

            elif data == "menu_newgame":
                pending_new_game.add(uid)
                await bot.send_message(chat_id, MESSAGES["newgame_prompt"])

            elif data == "menu_mytargets":
                fake_msg = types.Message(
                    message_id=callback_query.message.message_id,
                    date=msg_date_int,
                    chat=callback_query.message.chat,
                    from_user=callback_query.from_user,
                    text="/mytargets"
                )
                await cmd_mytargets(fake_msg)

            elif data == "menu_mygames":
                fake_msg = types.Message(
                    message_id=callback_query.message.message_id,
                    date=msg_date_int,
                    chat=callback_query.message.chat,
                    from_user=callback_query.from_user,
                    text="/mygames"
                )
                await cmd_mygames(fake_msg)

            elif data == "menu_players":
                fake_msg = types.Message(
                    message_id=callback_query.message.message_id,
                    date=msg_date_int,
                    chat=callback_query.message.chat,
                    from_user=callback_query.from_user,
                    text="/players"
                )
                await cmd_players(fake_msg)

            elif data == "menu_status":
                fake_msg = types.Message(
                    message_id=callback_query.message.message_id,
                    date=msg_date_int,
                    chat=callback_query.message.chat,
                    from_user=callback_query.from_user,
                    text="/status"
                )
                await cmd_status(fake_msg)

            elif data == "menu_startgame":
                fake_msg = types.Message(
                    message_id=callback_query.message.message_id,
                    date=msg_date_int,
                    chat=callback_query.message.chat,
                    from_user=callback_query.from_user,
                    text="/startgame"
                )
                await cmd_startgame(fake_msg)

            elif data == "menu_finishgame":
                fake_msg = types.Message(
                    message_id=callback_query.message.message_id,
                    date=msg_date_int,
                    chat=callback_query.message.chat,
                    from_user=callback_query.from_user,
                    text="/finishgame"
                )
                await cmd_finishgame(fake_msg)

            await bot.answer_callback_query(callback_query.id)

        # -------------------- Обработка текста --------------------

        @dp.message_handler()
        async def handle_text(message: types.Message):
            uid = message.from_user.id
            text = (message.text or "").strip()

            if uid in pending_new_game:
                pending_new_game.remove(uid)

                game_name = text[:200].strip()
                if not game_name:
                    await bot.send_message(message.chat.id, "❌ Название не может быть пустым.")
                    return

                creator_tg = (message.from_user.username or "").strip() or None
                creator_full = " ".join(filter(None, [message.from_user.first_name, message.from_user.last_name])).strip() or creator_tg or str(uid)

                try:
                    g = GameManager.create_game(uid, creator_full, game_name, creator_tg)
                    # проверка, что участник создан
                    db = SessionLocal()
                    try:
                        exists = db.query(Participant).filter(Participant.game_id == g["id"], Participant.user_id == uid).first()
                        if not exists:
                            logger.warning("After create_game participant missing for game=%s user=%s", g["id"], uid)
                        else:
                            logger.info("Participant created OK for game=%s user=%s", g["id"], uid)
                    finally:
                        db.close()

                    await bot.send_message(
                        message.chat.id,
                        MESSAGES["game_created"].format(
                            name=g["name"],
                            code=g["id"],
                            bot=bot_username
                        ),
                        reply_markup=main_menu_keyboard(is_admin=True)
                    )
                except Exception as e:
                    logger.exception("Error creating game: %s", e)
                    await bot.send_message(message.chat.id, "❌ Не удалось создать игру.")
                return

            # Если текст похож на код игры
            if len(text) == 8 and text.isalnum():
                await bot.send_message(
                    message.chat.id,
                    f"🔍 Похоже на код игры.\n"
                    f"Присоединиться: https://t.me/{bot_username}?start=join_{text.upper()}"
                )
                return

            await bot.send_message(message.chat.id, MESSAGES["unknown_command"])

        # -------------------- Очередь апдейтов --------------------

        async def process_queue():
            logger.info("Aiogram worker started")
            while True:
                try:
                    update_data = update_queue.get(timeout=1)
                except queue.Empty:
                    await asyncio.sleep(0.1)
                    continue

                try:
                    update = types.Update(**update_data)
                    await dp.process_update(update)
                except Exception as e:
                    logger.exception("Error processing update: %s", e)
                finally:
                    try:
                        update_queue.task_done()
                    except Exception:
                        pass

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.create_task(process_queue())

        try:
            loop.run_forever()
        finally:
            # корректное закрытие сессии бота
            try:
                loop.run_until_complete(bot.get_session())
            except Exception:
                pass
            try:
                loop.run_until_complete(bot.session.close())
            except Exception:
                pass

    # Запускаем воркер в отдельном потоке
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    logger.info("Background worker thread started")
    return update_queue
