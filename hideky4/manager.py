# app/manager.py
# Логика управления играми: создание, присоединение, жеребьёвка, цели, информация

import logging
import random
from datetime import datetime

from hideky4.database import SessionLocal
from hideky4.models import Game, Participant
from hideky4.utils import generate_game_id

logger = logging.getLogger(__name__)


class GameManager:

    @staticmethod
    def create_game(creator_id: int, creator_name: str, game_name: str, creator_tg: str | None = None, budget: str | None = None):
        """Создаёт новую игру и добавляет создателя как участника."""
        db = SessionLocal()
        try:
            game_id = generate_game_id()
            # invite_link формируем как шаблон; worker подставит реальный bot_username
            invite_link = f"https://t.me/REPLACE_WITH_BOT_USERNAME?start=join_{game_id}"

            game = Game(
                id=game_id,
                name=game_name,
                admin_id=creator_id,
                admin_username=creator_name,
                chat_id=str(creator_id),
                is_active=True,
                is_started=False,
                created_at=datetime.utcnow(),
                gift_price=budget or "Без ограничений"
            )
            db.add(game)

            participant = Participant(
                game_id=game_id,
                user_id=creator_id,
                username=creator_tg,
                full_name=creator_name
            )
            db.add(participant)

            db.commit()
            logger.info("game_created: %s by %s", game_id, creator_id)

            return {
                "id": game_id,
                "name": game_name,
                "creator_id": creator_id,
                "creator_name": creator_name,
                "budget": budget or "Без ограничений",
                "invite_link": invite_link
            }

        except Exception as e:
            db.rollback()
            logger.exception("Error create_game: %s", e)
            raise
        finally:
            db.close()

    @staticmethod
    def join_game(game_id: str, user_id: int, tg_username: str | None, full_name: str):
        """Присоединяет пользователя к игре."""
        db = SessionLocal()
        try:
            game = db.query(Game).filter(Game.id == game_id).first()
            if not game:
                return False, "❌ Игра не найдена"

            if game.is_started:
                return False, "⏳ Игра уже началась"

            exists = db.query(Participant).filter(
                Participant.game_id == game_id,
                Participant.user_id == user_id
            ).first()

            if exists:
                return False, "🎅 Вы уже участвуете в этой игре"

            participant = Participant(
                game_id=game_id,
                user_id=user_id,
                username=tg_username,
                full_name=full_name
            )
            db.add(participant)
            db.commit()

            logger.info("player_joined: game=%s user=%s", game_id, user_id)
            return True, "🎉 Вы присоединились к праздничной игре!"

        except Exception as e:
            db.rollback()
            logger.exception("Error join_game: %s", e)
            return False, "❌ Ошибка при присоединении"
        finally:
            db.close()

    @staticmethod
    def start_game(game_id: str, creator_id: int):
        """Запускает жеребьёвку и сохраняет назначения в БД."""
        db = SessionLocal()
        try:
            game = db.query(Game).filter(Game.id == game_id).first()
            if not game:
                return False, "❌ Игра не найдена"

            if game.admin_id != creator_id:
                return False, "👑 Только создатель может начать игру"

            if game.is_started:
                return False, "⏳ Игра уже началась"

            participants = db.query(Participant).filter(
                Participant.game_id == game_id
            ).all()

            if len(participants) < 2:
                return False, "🎁 Нужно минимум 2 участника"

            # Собираем список user_id и перемешиваем
            user_ids = [p.user_id for p in participants]
            random.shuffle(user_ids)

            # Назначаем круговую жеребьёвку: giver -> next user
            assignments = []
            for i, giver_id in enumerate(user_ids):
                receiver_id = user_ids[(i + 1) % len(user_ids)]
                giver_rec = db.query(Participant).filter(
                    Participant.game_id == game_id,
                    Participant.user_id == giver_id
                ).first()

                if not giver_rec:
                    logger.warning("start_game: participant not found giver=%s game=%s", giver_id, game_id)
                    continue

                # Присваиваем target_id
                giver_rec.target_id = receiver_id

                # Попробуем заполнить дополнительные поля, если они есть в модели
                receiver_part = db.query(Participant).filter(
                    Participant.game_id == game_id,
                    Participant.user_id == receiver_id
                ).first()
                if receiver_part:
                    if hasattr(giver_rec, "target_username"):
                        try:
                            giver_rec.target_username = receiver_part.username
                        except Exception:
                            pass
                    if hasattr(giver_rec, "target_full_name"):
                        try:
                            giver_rec.target_full_name = receiver_part.full_name
                        except Exception:
                            pass

                assignments.append((giver_id, receiver_id))

            # Помечаем игру как начатую
            game.is_started = True
            game.started_at = datetime.utcnow()

            db.commit()

            # Логируем все пары для отладки и мониторинга
            for giver_id, receiver_id in assignments:
                logger.info("pair_assigned: game=%s santa=%s receiver=%s", game_id, giver_id, receiver_id)

            logger.info("game_started: %s", game_id)
            return True, "🎄 Игра началась! Тайные Санты распределены 🎅"

        except Exception as e:
            db.rollback()
            logger.exception("Error start_game: %s", e)
            return False, "❌ Ошибка при старте игры"
        finally:
            db.close()

    @staticmethod
    def finish_game(game_id: str, user_id: int):
        """Завершает игру."""
        db = SessionLocal()
        try:
            game = db.query(Game).filter(Game.id == game_id).first()
            if not game:
                return False, "❌ Игра не найдена"

            if game.admin_id != user_id:
                return False, "👑 Только создатель может завершить игру"

            if not game.is_started:
                return False, "⏳ Игра ещё не началась"

            game.is_active = False
            game.is_started = False

            db.commit()
            logger.info("game_finished: %s", game_id)
            return True, "✅ Игра завершена! Спасибо за участие 🎁"

        except Exception as e:
            db.rollback()
            logger.exception("Error finish_game: %s", e)
            return False, "❌ Ошибка при завершении игры"
        finally:
            db.close()

    @staticmethod
    def set_wishlist(user_id: int, wishlist_text: str):
        """Сохраняет пожелания участника."""
        db = SessionLocal()
        try:
            p = db.query(Participant).filter(
                Participant.user_id == user_id
            ).order_by(Participant.id.desc()).first()

            if not p:
                return False, "❌ Вы не участвуете в играх"

            game = db.query(Game).filter(Game.id == p.game_id).first()

            if not game or game.is_started:
                return False, "⏳ Нельзя менять пожелания после старта игры"

            p.wishlist = wishlist_text
            db.commit()

            logger.info("wishlist_saved: user=%s game=%s", user_id, p.game_id)
            return True, "📝 Пожелания сохранены!"

        except Exception as e:
            db.rollback()
            logger.exception("Error set_wishlist: %s", e)
            return False, "❌ Ошибка при сохранении пожеланий"
        finally:
            db.close()

    @staticmethod
    def get_my_targets(user_id: int):
        """Возвращает список целей пользователя во всех играх."""
        db = SessionLocal()
        try:
            rows = db.query(Participant).filter(
                Participant.user_id == user_id
            ).all()

            results = []

            for p in rows:
                game = db.query(Game).filter(Game.id == p.game_id).first()
                if not game or not game.is_started:
                    continue

                if not p.target_id:
                    results.append({
                        "game_id": p.game_id,
                        "game_name": game.name,
                        "target_id": None
                    })
                    continue

                target = db.query(Participant).filter(
                    Participant.game_id == p.game_id,
                    Participant.user_id == p.target_id
                ).first()

                if not target:
                    results.append({
                        "game_id": p.game_id,
                        "game_name": game.name,
                        "target_id": None
                    })
                    continue

                results.append({
                    "game_id": p.game_id,
                    "game_name": game.name,
                    "target_id": target.user_id,
                    "target_username": target.username,
                    "target_full_name": target.full_name,
                    "target_wishlist": target.wishlist or "Пожелания не указаны"
                })

            return results

        except Exception as e:
            logger.exception("Error get_my_targets: %s", e)
            return []
        finally:
            db.close()

    @staticmethod
    def get_game_info(game_id: str):
        """Возвращает полную информацию об игре."""
        db = SessionLocal()
        try:
            game = db.query(Game).filter(Game.id == game_id).first()
            if not game:
                return None

            participants = db.query(Participant).filter(
                Participant.game_id == game_id
            ).all()

            participants_info = []
            for p in participants:
                participants_info.append({
                    "user_id": p.user_id,
                    "username": p.username,
                    "full_name": p.full_name,
                    "has_wishlist": bool(p.wishlist)
                })

            return {
                "id": game.id,
                "name": game.name,
                "creator_id": game.admin_id,
                "creator_name": game.admin_username,
                "status": (
                    "active" if game.is_started else
                    ("waiting" if game.is_active else "finished")
                ),
                "budget": game.gift_price,
                "created_at": game.created_at.isoformat() if game.created_at else None,
                "participants": participants_info
            }

        except Exception as e:
            logger.exception("Error get_game_info: %s", e)
            return None
        finally:
            db.close()
