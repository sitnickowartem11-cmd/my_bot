# app/webhook_app.py
# Flask-приложение: webhook, статус, дамп, запуск aiogram worker

import os
import logging
from flask import Flask, request, jsonify

from hideky4.worker import start_worker
from hideky4.database import init_db, SessionLocal
from hideky4.models import Game, Participant

# ---------------------------------------------------------
# ЛОГИ
# ---------------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("santa")

# ---------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME")
ADMIN_ID = os.environ.get("ADMIN_ID")  # для /dump_games

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME is not set")

# Railway URL
WEBHOOK_HOST = os.environ.get("RAILWAY_STATIC_URL") or os.environ.get("WEBHOOK_HOST")
if not WEBHOOK_HOST:
    raise RuntimeError("WEBHOOK_HOST or RAILWAY_STATIC_URL must be set")

if not WEBHOOK_HOST.startswith("http"):
    WEBHOOK_HOST = "https://" + WEBHOOK_HOST

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

# ---------------------------------------------------------
# FLASK
# ---------------------------------------------------------

app = Flask(__name__)

# Версия приложения
@app.route("/version")
def version():
    return "VERSION_1.0.0_PROFESSIONAL_BUILD"

# ---------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ БД
# ---------------------------------------------------------

init_db()

# ---------------------------------------------------------
# ЗАПУСК AIOGRAM WORKER
# ---------------------------------------------------------

update_queue = start_worker(BOT_TOKEN, BOT_USERNAME)

# ---------------------------------------------------------
# WEBHOOK
# ---------------------------------------------------------

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    try:
        update_data = request.get_json()
        update_id = update_data.get("update_id", "unknown")

        update_queue.put(update_data)
        logger.info("📥 Update queued: %s", update_id)

        return jsonify({"status": "queued", "update_id": update_id})

    except Exception as e:
        logger.exception("Webhook error: %s", e)
        return jsonify({"status": "error"}), 500

# ---------------------------------------------------------
# СЕРВИСНЫЕ ЭНДПОЙНТЫ
# ---------------------------------------------------------

@app.route("/")
def index():
    return (
        f"🎅 Secret Santa Bot работает<br>"
        f"Webhook: {WEBHOOK_URL}<br><br>"
        f"<a href='/set_webhook'>Установить вебхук</a><br>"
        f"<a href='/delete_webhook'>Удалить вебхук</a><br>"
        f"<a href='/status'>Статус</a><br>"
        f"<a href='/version'>Версия</a><br>"
    )

@app.route("/set_webhook")
def set_webhook():
    from aiogram import Bot
    import asyncio

    async def _set():
        bot = Bot(token=BOT_TOKEN)
        try:
            await bot.set_webhook(WEBHOOK_URL)
        finally:
            session = await bot.get_session()
            await session.close()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_set())
        loop.close()

        return f"✅ Вебхук установлен: {WEBHOOK_URL}"

    except Exception as e:
        logger.exception("Error set_webhook: %s", e)
        return f"❌ Ошибка: {e}"

@app.route("/delete_webhook")
def delete_webhook():
    from aiogram import Bot
    import asyncio

    async def _delete():
        bot = Bot(token=BOT_TOKEN)
        try:
            await bot.delete_webhook()
        finally:
            session = await bot.get_session()
            await session.close()

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_delete())
        loop.close()

        return "✅ Вебхук удалён"

    except Exception as e:
        logger.exception("Error delete_webhook: %s", e)
        return f"❌ Ошибка: {e}"

@app.route("/status")
def status():
    db = SessionLocal()
    try:
        total_games = db.query(Game).count()
        active_games = db.query(Game).filter(Game.is_started == True).count()
        waiting_games = db.query(Game).filter(Game.is_started == False, Game.is_active == True).count()
        finished_games = db.query(Game).filter(Game.is_active == False).count()
        total_players = db.query(Participant).distinct(Participant.user_id).count()

        return jsonify({
            "service": "Secret Santa Bot",
            "status": "online",
            "webhook_url": WEBHOOK_URL,
            "background_worker": True,
            "queue_size": update_queue.qsize(),
            "total_games": total_games,
            "active_games": active_games,
            "waiting_games": waiting_games,
            "finished_games": finished_games,
            "total_players": total_players
        })

    finally:
        db.close()

@app.route("/dump_games")
def dump_games():
    caller = request.args.get("admin_id")

    if ADMIN_ID and str(caller) != str(ADMIN_ID):
        return jsonify({"error": "forbidden"}), 403

    db = SessionLocal()
    try:
        games = []
        for g in db.query(Game).all():
            participants = []
            for p in db.query(Participant).filter(Participant.game_id == g.id).all():
                participants.append({
                    "user_id": p.user_id,
                    "username": p.username,
                    "full_name": p.full_name,
                    "wishlist": p.wishlist,
                    "target_id": p.target_id
                })

            games.append({
                "id": g.id,
                "name": g.name,
                "admin_id": g.admin_id,
                "admin_username": g.admin_username,
                "is_active": g.is_active,
                "is_started": g.is_started,
                "created_at": g.created_at.isoformat() if g.created_at else None,
                "started_at": g.started_at.isoformat() if g.started_at else None,
                "participants": participants
            })

        return jsonify({"games": games})

    finally:
        db.close()

# ---------------------------------------------------------
# ЗАПУСК (локально)
# ---------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("Starting Flask app on port %s", port)
    app.run(host="0.0.0.0", port=port)