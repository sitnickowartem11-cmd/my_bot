# app/models.py
# SQLAlchemy модели: Game и Participant

from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Text, ForeignKey
)
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


class Game(Base):
    __tablename__ = "games"

    id = Column(String(50), primary_key=True)  # код игры (ABC123XY)
    name = Column(String(200), nullable=False)

    admin_id = Column(Integer, nullable=False)
    admin_username = Column(String(200), nullable=True)

    chat_id = Column(String(50), nullable=True)

    is_active = Column(Boolean, default=True)
    is_started = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)

    gift_price = Column(String(100), nullable=True)
    wishlist = Column(Text, nullable=True)

    # связь с участниками
    participants = relationship(
        "Participant",
        back_populates="game",
        cascade="all, delete-orphan"
    )


class Participant(Base):
    __tablename__ = "participants"

    id = Column(Integer, primary_key=True)

    game_id = Column(String(50), ForeignKey("games.id"), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)

    username = Column(String(100), nullable=True)
    full_name = Column(String(200), nullable=True)

    wishlist = Column(Text, nullable=True)

    # кому этот участник дарит
    target_id = Column(Integer, nullable=True, index=True)

    # связь с игрой
    game = relationship("Game", back_populates="participants")
