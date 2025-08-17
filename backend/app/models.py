from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from sqlalchemy import (
    Column,
    String,
    Integer,
    BigInteger,
    Float,
    Numeric,
    Boolean,
    DateTime,
    ForeignKey,
    Enum,
    JSON,
    UniqueConstraint,
    Index,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import declarative_base, relationship
from .config import get_settings

settings = get_settings()

# Базовый класс для ORM
Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------
# Общие перечисления
# ------------------------------------------------------------

class WalletChain(str, enum.Enum):
    TON = "TON"
    USDT = "USDT"  # для on/off-ramp, оплата пакетов, VIP
    EFHC = "EFHC"  # логический маркер существующего токена (если потребуется)


class PanelStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"   # автоматически перемещается в архив
    ARCHIVED = "ARCHIVED" # защита от повторной активации


class TransactionType(str, enum.Enum):
    DEPOSIT = "DEPOSIT"                  # пополнение EFHC (внешнее зачисление после проверки)
    WITHDRAWAL = "WITHDRAWAL"            # заявка на вывод EFHC
    PANEL_PURCHASE = "PANEL_PURCHASE"    # покупка панели (–100 EFHC)
    REFERRAL_BONUS = "REFERRAL_BONUS"    # реферальный бонус (+)
    EXCHANGE_CREDIT = "EXCHANGE_CREDIT"  # кВт → EFHC (зачисление)
    EXCHANGE_DEBIT = "EXCHANGE_DEBIT"    # резерв на обмен (если нужно)
    SHOP_PURCHASE = "SHOP_PURCHASE"      # покупка пакета в Shop (EFHC/TON/USDT)
    LOTTERY_TICKET = "LOTTERY_TICKET"    # покупка билетов (–EFHC)
    LOTTERY_PRIZE = "LOTTERY_PRIZE"      # приз лотереи (+EFHC, VIP, PANEL)
    ADJUSTMENT = "ADJUSTMENT"            # ручная корректировка админом


class LotteryPrizeType(str, enum.Enum):
    VIP_NFT = "VIP_NFT"
    PANEL = "PANEL"
    EFHC = "EFHC"  # на будущее (возможный денежный приз EFHC)


class AdminRole(str, enum.Enum):
    OWNER = "OWNER"      # главный админ (вы)
    MANAGER = "MANAGER"  # ограниченный админ (гибко настраивается)
    ANALYST = "ANALYST"  # только чтение/метрики


# ------------------------------------------------------------
# Схемы БД (PostgreSQL schemas)
# ------------------------------------------------------------

SCHEMA_CORE = settings.DB_SCHEMA_CORE
SCHEMA_REF = settings.DB_SCHEMA_REFERRAL
SCHEMA_ADMIN = settings.DB_SCHEMA_ADMIN
SCHEMA_LOTTERY = settings.DB_SCHEMA_LOTTERY


# ------------------------------------------------------------
# CORE: Пользователи, кошельки, балансы
# ------------------------------------------------------------

class User(Base):
    """
    Пользователь EFHC-бота.
    - EFHC и kWh — разные балансы.
    - active_user -> True после первой покупки панели (навсегда).
    - VIP проверяется по NFT ежедневно (00:00 UTC); хранится флаг последней проверки.
    """
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("telegram_id", name="uq_users_telegram_id"),
        {"schema": SCHEMA_CORE},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    telegram_id = Column(BigInteger, nullable=False, index=True)
    username = Column(String(64), nullable=True)
    language = Column(String(8), nullable=False, default=settings.DEFAULT_LANG)

    # Балансы
    balance_efhc = Column(Numeric(20, settings.EFHC_DECIMALS), nullable=False, default=0)
    balance_kwh_total_generated = Column(Numeric(20, settings.KWH_DECIMALS), nullable=False, default=0)  # только панели
    balance_kwh_available_for_exchange = Column(Numeric(20, settings.KWH_DECIMALS), nullable=False, default=0)

    # Статусы
    is_active_user = Column(Boolean, nullable=False, default=False)  # true навсегда после 1-й панели
    is_vip = Column(Boolean, nullable=False, default=False)          # результат последней проверки NFT
    last_nft_check_at = Column(DateTime(timezone=True), nullable=True)

    # Рефералы
    referred_by_user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_REF}.referral_links.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    wallets = relationship("UserWallet", back_populates="user", cascade="all, delete-orphan")
    panels = relationship("Panel", back_populates="user", cascade="all, delete-orphan")
    accruals = relationship("EnergyAccrual", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<User tg={self.telegram_id} @{self.username} efhc={self.balance_efhc} kwh={self.balance_kwh_total_generated}>"


class UserWallet(Base):
    """
    Привязанные кошельки пользователя. Для TON/USDT и т.д.
    - memo/comment при пополнении заполняется автоматически (telegram_id).
    """
    __tablename__ = "user_wallets"
    __table_args__ = (
        UniqueConstraint("user_id", "chain", name="uq_wallet_user_chain"),
        {"schema": SCHEMA_CORE},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_CORE}.users.id"), nullable=False)

    chain = Column(Enum(WalletChain), nullable=False)
    address = Column(String(255), nullable=False)  # валидируется на фронте/бэке
    memo_template = Column(String(128), nullable=True)  # формируется из telegram_id

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    user = relationship("User", back_populates="wallets")


# ------------------------------------------------------------
# CORE: Панели и начисления
# ------------------------------------------------------------

class Panel(Base):
    """
    Виртуальная панель.
    - Стоимость 100 EFHC (всегда).
    - Срок 180 дней.
    - Генерация:
        без VIP: 0.598 кВт/сутки
        с VIP:  0.64  кВт/сутки (упрощённый фикс)
    - При окончании срока — статус EXPIRED, далее ARCHIVED.
    """
    __tablename__ = "panels"
    __table_args__ = (
        Index("ix_panels_user_status", "user_id", "status"),
        {"schema": SCHEMA_CORE},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_CORE}.users.id"), nullable=False)

    start_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    end_at = Column(DateTime(timezone=True), nullable=False)  # start_at + 180 дней
    status = Column(Enum(PanelStatus), nullable=False, default=PanelStatus.ACTIVE)

    # кешируем номер/метку для удобства UI (например: Panel 10.07.2025)
    label = Column(String(64), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    user = relationship("User", back_populates="panels")

    def days_left(self) -> int:
        if self.status != PanelStatus.ACTIVE:
            return 0
        delta = self.end_at - utcnow()
        return max(0, delta.days)

    def is_expired(self) -> bool:
        return utcnow() >= self.end_at


class EnergyAccrual(Base):
    """
    Ежедневные начисления энергии (kWh) для пользователя.
    - Пишется суммарная генерация всех активных панелей за день с учётом VIP на дату.
    - Используется для статистики и рейтинга.
    """
    __tablename__ = "energy_accruals"
    __table_args__ = (
        Index("ix_accruals_user_day", "user_id", "accrual_date", unique=True),
        {"schema": SCHEMA_CORE},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_CORE}.users.id"), nullable=False)

    accrual_date = Column(DateTime(timezone=True), nullable=False)  # полуночь UTC (дата начисления)
    amount_kwh = Column(Numeric(20, settings.KWH_DECIMALS), nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    user = relationship("User", back_populates="accruals")


# ------------------------------------------------------------
# CORE: Транзакции EFHC
# ------------------------------------------------------------

class Transaction(Base):
    """
    Учёт всех EFHC-операций.
    EFHC «не исчезают», а перемещаются между пользователями и админ-банком (виртуально).
    """
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_user_created", "user_id", "created_at"),
        {"schema": SCHEMA_CORE},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_CORE}.users.id"), nullable=False)

    tx_type = Column(Enum(TransactionType), nullable=False)
    amount_efhc = Column(Numeric(20, settings.EFHC_DECIMALS), nullable=False)  # +/-
    memo = Column(String(255), nullable=True)  # например, package_id, order_id, или 'exchange kWh->EFHC'

    # Внешние поля аудита/связки: хеш транзакции сети и т.п. (если есть)
    external_tx_id = Column(String(255), nullable=True)
    external_chain = Column(Enum(WalletChain), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    user = relationship("User", back_populates="transactions")


# ------------------------------------------------------------
# REFERRALS: Реферальная система
# ------------------------------------------------------------

class ReferralLink(Base):
    """
    Узел рефералки — у кого есть собственная ссылка-инвайт.
    inviter_user_id -> владелец ссылки.
    """
    __tablename__ = "referral_links"
    __table_args__ = (
        UniqueConstraint("inviter_user_id", name="uq_ref_link_inviter"),
        {"schema": SCHEMA_REF},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inviter_user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_CORE}.users.id"), nullable=False)

    code = Column(String(64), nullable=False, unique=True)  # человекочитаемый код или UUID
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    inviter = relationship("User", foreign_keys=[inviter_user_id])


class ReferralRelation(Base):
    """
    Связь "кто кого пригласил".
    - active = True, если приглашённый купил хотя бы 1 панель (навсегда).
    - bonuses_paid_efhc — сколько всего EFHC выплачено за этого реферала.
    """
    __tablename__ = "referral_relations"
    __table_args__ = (
        UniqueConstraint("inviter_user_id", "invitee_user_id", name="uq_ref_relation_pair"),
        {"schema": SCHEMA_REF},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    inviter_user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_CORE}.users.id"), nullable=False)
    invitee_user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_CORE}.users.id"), nullable=False)

    active = Column(Boolean, nullable=False, default=False)  # становится True после первой покупки панели
    bonuses_paid_efhc = Column(Numeric(20, settings.EFHC_DECIMALS), nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class ReferralMilestone(Base):
    """
    Учёт "разовых" пороговых бонусов (10/100/1000/3000/10000 активных рефералов).
    Чтобы один и тот же порог не сработал дважды для одного пользователя.
    """
    __tablename__ = "referral_milestones"
    __table_args__ = (
        UniqueConstraint("user_id", "threshold", name="uq_ref_milestone_user_threshold"),
        {"schema": SCHEMA_REF},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_CORE}.users.id"), nullable=False)
    threshold = Column(Integer, nullable=False)  # 10, 100, 1000, 3000, 10000
    bonus_efhc = Column(Numeric(20, settings.EFHC_DECIMALS), nullable=False)  # 1, 10, 100, 300, 1000
    granted_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


# ------------------------------------------------------------
# ADMIN: Магазин, глобальные настройки, админы
# ------------------------------------------------------------

class ShopPackage(Base):
    """
    Пакеты магазина (управляются из админ-панели).
    Примеры:
      - 10 EFHC за 0.8 TON
      - VIP NFT за 50 USDT
    """
    __tablename__ = "shop_packages"
    __table_args__ = (
        UniqueConstraint("package_id", name="uq_shop_package_id"),
        {"schema": SCHEMA_ADMIN},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    package_id = Column(String(64), nullable=False)  # постоянный ID (например, efhc_100_usdt)
    title = Column(String(128), nullable=False)
    pay_asset = Column(String(16), nullable=False)   # "EFHC" | "TON" | "USDT"
    price = Column(Numeric(20, 6), nullable=False)   # цена в pay_asset
    payload_json = Column(JSON, nullable=True)       # произвольные доп. параметры

    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class FeatureFlag(Base):
    """
    Флаги функционала, переключаемые в админке:
      - lottery_enabled
      - shop_enabled
      - exchange_enabled
      - referrals_enabled
      - и т.п.
    """
    __tablename__ = "feature_flags"
    __table_args__ = (
        UniqueConstraint("key", name="uq_feature_flag_key"),
        {"schema": SCHEMA_ADMIN},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key = Column(String(64), nullable=False)
    value_bool = Column(Boolean, nullable=True)
    value_text = Column(Text, nullable=True)
    value_json = Column(JSON, nullable=True)

    updated_by_admin_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_ADMIN}.admins.id"), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class Admin(Base):
    """
    Администратор (управление из панели).
    - Двухфактор: Telegram ID + NFT-ключ (если включено).
    - Пермишены через JSON/флаги.
    """
    __tablename__ = "admins"
    __table_args__ = (
        UniqueConstraint("telegram_id", name="uq_admin_telegram_id"),
        {"schema": SCHEMA_ADMIN},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    role = Column(Enum(AdminRole), nullable=False, default=AdminRole.MANAGER)
    permissions_json = Column(JSON, nullable=True)  # гибкие галочки для функционала (как вы просили)

    # NFT-ключ (если включено ADMIN_ENFORCE_NFT)
    nft_required = Column(Boolean, nullable=False, default=True)
    enabled = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


# ------------------------------------------------------------
# LOTTERY: Розыгрыши (VIP/Панель), билеты, победители
# ------------------------------------------------------------

class Lottery(Base):
    """
    Розыгрыш:
      - prize_type: VIP_NFT | PANEL
      - target_participants: 500 (VIP) или 200 (панель) — задаётся в админке
      - enabled: активен/неактивен
      - auto_draw: розыгрыш запускается автоматически при достижении лимита
    """
    __tablename__ = "lotteries"
    __table_args__ = (
        Index("ix_lottery_enabled", "enabled"),
        {"schema": SCHEMA_LOTTERY},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = Column(String(64), nullable=False, unique=True)  # постоянный ID лотереи (например, "lottery_vip_march")
    title = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)

    prize_type = Column(Enum(LotteryPrizeType), nullable=False)
    target_participants = Column(Integer, nullable=False)  # 500 / 200
    ticket_price_efhc = Column(Numeric(20, settings.EFHC_DECIMALS), nullable=False, default=settings.LOTTERY_TICKET_PRICE_EFHC)
    max_tickets_per_user = Column(Integer, nullable=False, default=settings.LOTTERY_MAX_TICKETS_PER_USER)

    enabled = Column(Boolean, nullable=False, default=True)
    auto_draw = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    tickets = relationship("LotteryTicket", back_populates="lottery", cascade="all, delete-orphan")
    winners = relationship("LotteryWinner", back_populates="lottery", cascade="all, delete-orphan")

    def is_full(self) -> bool:
        return len(self.tickets) >= self.target_participants

    def participants_count(self) -> int:
        # уникальные user_id
        return len({t.user_id for t in self.tickets})


class LotteryTicket(Base):
    """
    Билет розыгрыша.
    - Стоимость 1 EFHC.
    - Ограничение 10 билетов на пользователя в рамках одной лотереи.
    """
    __tablename__ = "lottery_tickets"
    __table_args__ = (
        UniqueConstraint("lottery_id", "user_id", "serial", name="uq_ticket_per_user_serial"),
        Index("ix_ticket_lottery_user", "lottery_id", "user_id"),
        {"schema": SCHEMA_LOTTERY},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lottery_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_LOTTERY}.lotteries.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_CORE}.users.id"), nullable=False)

    serial = Column(Integer, nullable=False)  # порядковый номер билета у пользователя (1..10)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    lottery = relationship("Lottery", back_populates="tickets")
    user = relationship("User")


class LotteryWinner(Base):
    """
    Победитель розыгрыша.
    - Для VIP_NFT: создаётся задача на ручную выдачу NFT (админ-панель получит уведомление).
    - Для PANEL: автоматически добавляем 1 панель пользователю (через бизнес-логику/сервис).
    """
    __tablename__ = "lottery_winners"
    __table_args__ = (
        UniqueConstraint("lottery_id", "user_id", name="uq_winner_lottery_user"),
        {"schema": SCHEMA_LOTTERY},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lottery_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_LOTTERY}.lotteries.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_CORE}.users.id"), nullable=False)

    ticket_id = Column(UUID(as_uuid=True), ForeignKey(f"{SCHEMA_LOTTERY}.lottery_tickets.id"), nullable=True)
    prize_type = Column(Enum(LotteryPrizeType), nullable=False)
    granted = Column(Boolean, nullable=False, default=False)  # отмечаем, когда приз выдан

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    granted_at = Column(DateTime(timezone=True), nullable=True)

    lottery = relationship("Lottery", back_populates="winners")
    ticket = relationship("LotteryTicket")


# ------------------------------------------------------------
# Индексы для рейтинга/метрик
# ------------------------------------------------------------

Index("ix_users_energy_sort", User.balance_kwh_total_generated, schema=SCHEMA_CORE)
Index("ix_users_referral_sort", User.telegram_id, schema=SCHEMA_CORE)  # для быстрых выборок; реальные метрики из REFERRALS


# ------------------------------------------------------------
# Примечания по миграциям
# ------------------------------------------------------------
# 1) Мы используем разные PostgreSQL-схемы: efhc_core, efhc_referrals, efhc_admin, efhc_lottery.
#    В Supabase создайте эти схемы вручную (или через миграции).
# 2) Для Alembic (если будете подключать) указывайте schema в __table_args__ как здесь.
# 3) Денежные/энергетические величины — Numeric с округлением до 3 знаков, как вы требовали.
# 4) Архивация панелей:
#    - По cron (scheduler) перевести ACTIVE -> EXPIRED при end_at <= now, после чего бизнес-логика относит её к архиву (ARCHIVED).
#    - В UI «Архив» — это панели со статусом EXPIRED/ARCHIVED.
# 5) Начисления:
#    - Ежедневно в 00:30 UTC пишем одну запись EnergyAccrual с итогом за день.
#    - Обновляем balance_kwh_total_generated и balance_kwh_available_for_exchange у User.
# 6) Лотереи:
#    - В админке создаёте записи Lottery, включаете/выключаете.
#    - В фоне/по событию проводится draw (в lottery.py), заполняется LotteryWinner, выдаётся приз.
# 7) Рефералы:
#    - При первой покупке панели помечаем ReferralRelation.active = True.
#    - Начисляем 0.1 EFHC и проверяем пороги (10/100/1000/3000/10000) — записываем в ReferralMilestone.
