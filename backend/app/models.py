# 📂 backend/app/models.py — SQLAlchemy ORM модели EFHC
# -----------------------------------------------------------------------------
# Что здесь:
#   • Декларативные модели для всех наших таблиц в разных схемах:
#       - efhc_core: users, balances, user_panels, user_vip, (лог ton_events_log — описан для чтения)
#       - efhc_referrals: referrals, referral_stats
#       - efhc_tasks: tasks, user_tasks
#       - efhc_lottery: lotteries, lottery_tickets
#       - efhc_admin: admin_nft_whitelist
#   • Используем схему из config.py (settings.DB_SCHEMA_*)
#   • Типы и ограничения совместимы с нашей бизнес-логикой
#
# Примечания:
#   • В этом проекте миграции лучше вести Alembic'ом, но модели нужны для выборок/CRUD.
#   • Таблицы ton_events_log и user_vip мы также описываем ORM-классами, хотя создавались
#     через raw SQL в ton_integration.py (ensure_ton_tables) — это нормально.
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import (
    Column, Integer, BigInteger, String, DateTime, Boolean, Numeric, Text, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, relationship

from .config import get_settings

settings = get_settings()

# Базовый класс ORM
Base = declarative_base()

# Удобные алиасы схем (из конфига)
S_CORE = settings.DB_SCHEMA_CORE
S_ADMIN = settings.DB_SCHEMA_ADMIN
S_REF = settings.DB_SCHEMA_REFERRAL
S_TASK = settings.DB_SCHEMA_TASKS
S_LOT  = settings.DB_SCHEMA_LOTTERY


# ---------------------------------------------------------------------
# efhc_core.users — пользователи (Telegram)
# ---------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": S_CORE}

    telegram_id = Column(BigInteger, primary_key=True, index=True)
    username = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Связи (не обязательны, но удобны)
    balance = relationship("Balance", uselist=False, back_populates="user", cascade="all, delete-orphan")
    panels = relationship("UserPanel", back_populates="user", cascade="all, delete-orphan")


# ---------------------------------------------------------------------
# efhc_core.balances — внутренний баланс EFHC/bonus/kWh
# ---------------------------------------------------------------------
class Balance(Base):
    __tablename__ = "balances"
    __table_args__ = {"schema": S_CORE}

    telegram_id = Column(BigInteger, ForeignKey(f"{S_CORE}.users.telegram_id", ondelete="CASCADE"), primary_key=True)
    efhc = Column(Numeric(30, 3), nullable=False, default=0)
    bonus = Column(Numeric(30, 3), nullable=False, default=0)
    kwh  = Column(Numeric(30, 3), nullable=False, default=0)

    user = relationship("User", back_populates="balance")


# ---------------------------------------------------------------------
# efhc_core.user_panels — купленные панели
# ---------------------------------------------------------------------
class UserPanel(Base):
    __tablename__ = "user_panels"
    __table_args__ = {"schema": S_CORE}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{S_CORE}.users.telegram_id", ondelete="CASCADE"), index=True, nullable=False)

    price_eFHC = Column(Numeric(30, 3), nullable=False, default=100)
    purchased_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)  # рассчитываем: purchased_at + PANEL_LIFESPAN_DAYS
    active = Column(Boolean, default=True, nullable=False)

    # можно хранить фактическую норму генерации (если нужна статистика)
    daily_gen_kwh = Column(Numeric(30, 3), nullable=False, default=0.598)

    user = relationship("User", back_populates="panels")


# ---------------------------------------------------------------------
# efhc_core.user_vip — внутренний VIP-флаг пользователя
# (НЕ админский доступ, админ доступ по NFT whitelist в efhc_admin.admin_nft_whitelist)
# ---------------------------------------------------------------------
class UserVIP(Base):
    __tablename__ = "user_vip"
    __table_args__ = {"schema": S_CORE}

    telegram_id = Column(BigInteger, ForeignKey(f"{S_CORE}.users.telegram_id", ondelete="CASCADE"), primary_key=True)
    since = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------
# efhc_core.ton_events_log — лог входящих событий из TonAPI (для аудита)
# ---------------------------------------------------------------------
class TonEventLog(Base):
    __tablename__ = "ton_events_log"
    __table_args__ = {"schema": S_CORE}

    event_id = Column(String, primary_key=True)
    ts = Column(DateTime, default=datetime.utcnow, nullable=False)

    action_type = Column(String, nullable=True)      # "TonTransfer" / "JettonTransfer"
    asset = Column(String, nullable=True)            # "TON" / "EFHC" / "USDT" / "JETTON:addr"

    amount = Column(Numeric(30, 9), nullable=True)   # в единицах актива (TON 9 знаков, EFHC 3 и т.д.)
    decimals = Column(Integer, nullable=True)

    from_addr = Column(String, nullable=True)
    to_addr = Column(String, nullable=True)

    memo = Column(Text, nullable=True)

    telegram_id = Column(BigInteger, nullable=True)  # из memo, если был
    parsed_amount_efhc = Column(Numeric(30, 3), nullable=True)  # сколько EFHC мы зачислили (если применимо)
    vip_requested = Column(Boolean, default=False, nullable=False)

    processed = Column(Boolean, default=True, nullable=False)
    processed_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------
# efhc_referrals.referrals — связи приглашений
# ---------------------------------------------------------------------
class Referral(Base):
    __tablename__ = "referrals"
    __table_args__ = {"schema": S_REF, UniqueConstraint("inviter_id", "invitee_id", name="uq_ref_pair")}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    inviter_id = Column(BigInteger, index=True, nullable=False)
    invitee_id = Column(BigInteger, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    active = Column(Boolean, default=False, nullable=False)


# ---------------------------------------------------------------------
# efhc_referrals.referral_stats — агрегаты/достижения
# ---------------------------------------------------------------------
class ReferralStat(Base):
    __tablename__ = "referral_stats"
    __table_args__ = {"schema": S_REF, UniqueConstraint("telegram_id", name="uq_ref_stat_tid")}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, index=True, nullable=False)
    direct_count = Column(Integer, default=0, nullable=False)
    bonuses_total = Column(Numeric(30, 3), default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------
# efhc_tasks.tasks — задания
# ---------------------------------------------------------------------
class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = {"schema": S_TASK}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    url = Column(Text, nullable=True)
    reward_bonus_efhc = Column(Numeric(30, 3), nullable=False, default=1.0)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------
# efhc_tasks.user_tasks — выполнение заданий пользователями
# ---------------------------------------------------------------------
class UserTask(Base):
    __tablename__ = "user_tasks"
    __table_args__ = {"schema": S_TASK, UniqueConstraint("task_id", "telegram_id", name="uq_user_task")}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(BigInteger, ForeignKey(f"{S_TASK}.tasks.id", ondelete="CASCADE"), nullable=False)
    telegram_id = Column(BigInteger, nullable=False)
    completed = Column(Boolean, default=False, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    # Если часто надо будет делать join — можно добавить relationship на Task


# ---------------------------------------------------------------------
# efhc_lottery.lotteries — активные розыгрыши
# ---------------------------------------------------------------------
class Lottery(Base):
    __tablename__ = "lotteries"
    __table_args__ = {"schema": S_LOT, UniqueConstraint("code", name="uq_lottery_code")}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(64), nullable=False, index=True)  # например "lottery_vip"
    title = Column(String(255), nullable=False)
    prize_type = Column(String(64), nullable=False)        # "VIP_NFT" / "PANEL" / "EFHC" etc.
    target_participants = Column(Integer, nullable=False, default=100)
    active = Column(Boolean, default=True, nullable=False)
    tickets_sold = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------
# efhc_lottery.lottery_tickets — билеты пользователей
# ---------------------------------------------------------------------
class LotteryTicket(Base):
    __tablename__ = "lottery_tickets"
    __table_args__ = {"schema": S_LOT}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    lottery_code = Column(String(64), nullable=False, index=True)  # связываемся по code
    telegram_id = Column(BigInteger, nullable=False, index=True)
    purchased_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------------------------------------------------
# efhc_admin.admin_nft_whitelist — NFT-токены, дающие доступ к админке
# -----------------------------------------------------------------------------
class AdminNFTWhitelist(Base):
    __tablename__ = "admin_nft_whitelist"
    __table_args__ = {"schema": S_ADMIN, UniqueConstraint("nft_address", name="uq_admin_nft")}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    nft_address = Column(String(200), nullable=False)  # адрес конкретного NFT-токена
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
