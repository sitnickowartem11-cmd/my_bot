# app/database.py
# Создаёт SQLAlchemy engine, сессии и базовый класс моделей.
# Полностью совместим с Railway PostgreSQL.

import os
import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session, declarative_base

logger = logging.getLogger(__name__)

# Получаем DATABASE_URL из переменных окружения
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set in environment variables")

# Railway иногда даёт postgres:// — SQLAlchemy требует postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Создаём движок
engine = create_engine(
    DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=3600,
    echo=False  # можно включить True для отладки SQL
)

# Создаём фабрику сессий
SessionLocal = scoped_session(
    sessionmaker(bind=engine, autocommit=False, autoflush=False)
)

# Базовый класс моделей
Base = declarative_base()

def init_db():
    """Создаёт таблицы, если их нет."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables created/checked successfully")
    except Exception as e:
        logger.exception("Error creating database tables: %s", e)
        raise
