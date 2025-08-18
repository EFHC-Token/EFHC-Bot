# 📂 backend/app/models.py — SQLAlchemy модели базы EFHC
# Здесь описаны все таблицы базы данных проекта EFHC:
# пользователи, панели, транзакции, рефералы, задания, розыгрыши, достижения и админ-доступ.

from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, ForeignKey, Text, JSON
from sqlalchemy.orm import relationship
from datetime import datetime
from .database import Base


# ======================
# 👤 Пользователи
# ======================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)  # внутренний ID
    telegram_id = Column(String, unique=True, index=True, nullable=False)  # Telegram ID
    username = Column(String, nullable=True)  # никнейм пользователя
    ton_wallet = Column(String, nullable=True)  # TON-кошелек
    created_at = Column(DateTime, default=datetime.utcnow)

    # Балансы
    balance_main = Column(Float, default=0.0)   # основной баланс EFHC
    balance_bonus = Column(Float, default=0.0)  # бонусные монеты (видны только в Панелях и Заданиях)
    balance_kwt = Column(Float, default=0.0)    # накопленные кВт

    # VIP и NFT
    is_vip = Column(Boolean, default=False)  # есть ли VIP NFT
    vip_checked_at = Column(DateTime, default=datetime.utcnow)  # дата последней проверки NFT

    # Рефералы
    referral_code = Column(String, unique=True, nullable=True)  # собственный код
    invited_by = Column(Integer, ForeignKey("users.id"), nullable=True)  # кто пригласил
    referrals = relationship("User", remote_side=[id])  # связь рефералов

    # Достижения
    current_level = Column(Integer, default=1)  # уровень пользователя
    total_kwt_generated = Column(Float, default=0.0)  # всего сгенерировано энергии

    # Админ-права (только для главного админа, остальные по NFT)
    is_admin = Column(Boolean, default=False)

    # Связь с другими таблицами
    panels = relationship("Panel", back_populates="owner")
    transactions = relationship("Transaction", back_populates="user")
    tasks = relationship("UserTask", back_populates="user")
    lottery_tickets = relationship("LotteryTicket", back_populates="user")


# ======================
# 🔋 Панели
# ======================
class Panel(Base):
    __tablename__ = "panels"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime)  # дата окончания срока службы

    daily_generation = Column(Float, default=0.598)  # генерация кВт/день
    is_active = Column(Boolean, default=True)

    owner = relationship("User", back_populates="panels")


# ======================
# 💸 Транзакции EFHC
# ======================
class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))

    amount = Column(Float, nullable=False)  # количество EFHC
    type = Column(String, nullable=False)   # тип ("deposit", "withdraw", "bonus", "panel_purchase", "transfer")
    comment = Column(Text, nullable=True)   # описание (например: "за покупку VIP NFT")
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="transactions")


# ======================
# 👥 Рефералы
# ======================
class ReferralBonus(Base):
    __tablename__ = "referral_bonus"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))        # кому начислен бонус
    invited_user_id = Column(Integer, ForeignKey("users.id"))  # кто совершил действие
    amount = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)


# ======================
# 📌 Задания
# ======================
class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)  # описание задания
    reward = Column(Float, default=0.0)     # награда в бонусных EFHC
    link = Column(String, nullable=True)    # ссылка на выполнение (например, Telegram канал)
    is_active = Column(Boolean, default=True)  # включено/выключено
    created_at = Column(DateTime, default=datetime.utcnow)


class UserTask(Base):
    __tablename__ = "user_tasks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    task_id = Column(Integer, ForeignKey("tasks.id"))
    status = Column(String, default="pending")  # pending, completed, approved
    completed_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="tasks")
    task = relationship("Task")


# ======================
# 🎲 Розыгрыши
# ======================
class Lottery(Base):
    __tablename__ = "lotteries"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)  # Название (например "NFT VIP")
    required_tickets = Column(Integer, nullable=False)  # сколько участников нужно
    reward_type = Column(String, nullable=False)  # "vip_nft" или "panel"
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    winner_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    tickets = relationship("LotteryTicket", back_populates="lottery")


class LotteryTicket(Base):
    __tablename__ = "lottery_tickets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    lottery_id = Column(Integer, ForeignKey("lotteries.id"))
    ticket_number = Column(Integer, nullable=False)  # номер билета для пользователя

    user = relationship("User", back_populates="lottery_tickets")
    lottery = relationship("Lottery", back_populates="tickets")


# ======================
# 🏆 Достижения
# ======================
class AchievementLog(Base):
    __tablename__ = "achievement_log"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    level = Column(Integer, nullable=False)  # какой уровень получен
    achieved_at = Column(DateTime, default=datetime.utcnow)


# ======================
# 🔑 Админ NFT-доступ
# ======================
class AdminNFT(Base):
    __tablename__ = "admin_nft"

    id = Column(Integer, primary_key=True, index=True)
    nft_address = Column(String, unique=True, nullable=False)  # какой NFT даёт права
    can_tasks = Column(Boolean, default=False)      # доступ к заданиям
    can_shop = Column(Boolean, default=False)       # доступ к магазину
    can_lottery = Column(Boolean, default=False)    # доступ к розыгрышам
    can_all = Column(Boolean, default=False)        # полный доступ
