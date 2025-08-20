# 📂 backend/app/models.py — SQLAlchemy ORM-модели (ПОЛНАЯ ВЕРСИЯ)
# -----------------------------------------------------------------------------
# Назначение:
#   • Единый набор ORM-моделей для ядра EFHC-проекта (схема efhc_core по умолчанию).
#   • Таблицы пользователей, балансов, панелей, статуса VIP (NFT), логов переводов EFHC,
#     заказов магазина (Shop), заявок на выдачу NFT, заявок на вывод, реферальных связей/бонусов,
#     истории/конфигурации банка (админ-счёт) и истории ставок генерации kWh.
#
# Бизнес-правила (резюме из переписки):
#   • 1 EFHC = 1 kWh. EFHC и kWh — оба хранятся в NUMERIC(30, 8) (8 знаков после точки).
#   • Бонус VIP/NFT = +7% → реализация через повышенную ставку kWh/сутки (0.640) vs обычная (0.598).
#     Фактические ставки настраиваются в админ-панели и вступают в силу в 01:00 следующего дня.
#   • Генерация kWh — «ленивая»: в balances.last_generated_at запоминаем последний апдейт, при
#     любом обращении считаем ∆t * (ставка/86400) * активные панели. Начинается сразу после покупки
#     первой панели (в shop_routes мы ставим last_generated_at=NOW()).
#   • Балансы:
#       - balances.efhc — текущий EFHC (NUMERIC(30,8)).
#       - balances.bonus_efhc — бонусные EFHC (NUMERIC(30,8)), тратятся ТОЛЬКО на панели.
#         При покупке панелей бонусные EFHC списываются у пользователя и возвращаются
#         на основной админский счёт (банк EFHC). Общего «бонусного» счёта не существует.
#       - balances.kwh — «доступные» kWh (NUMERIC(30,8)) — растут лениво.
#       - balances.last_generated_at — отметка времени для ленивой генерации.
#   • Банк EFHC (админ-счёт): telegram_id хранится в таблице AdminBankConfig и может мигрировать.
#     Все начисления/списания EFHC должны идти через efhc_transactions (с логом EFHCTransfersLog).
#   • VIP-статус включается/выключается исключительно после ежедневной проверки наличия NFT на кошельке пользователя.
#     Покупка VIP NFT в Shop создаёт заявку (manual_nft_requests), VIP НЕ включается автоматически.
#   • Панели: срок жизни = 180 дней. Покупка за EFHC/bonus_EFHC. На пользователя одновременно ≤ 1000 активных.
#   • Вывод EFHC: при создании заявки средства списываются user → банк (блокируются), далее админ одобряет/отправляет.
#   • Рефералы: одноразовый бонус за «активного» реферала (после покупки 1 панели), и награды по шкале
#     (10/100/1000/3000/10000 активных рефералов). Бонусы идут в bonus_efhc.
#
# Примечания:
#   • Здесь мы описываем модели для ORM. Создание/миграции таблиц выполняются через Alembic.
#   • Во всех местах, где фигурируют суммы EFHC/kWh — используем Decimal(30,8)-совместимые строки/Decimal.
#   • Внешние ручки (shop_routes, withdraw_routes и т.п.) используют RAW SQL / ORM — совместимо.
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    Column,
    BigInteger,
    Integer,
    String,
    Text,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
    Numeric,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship

from .config import get_settings

# -----------------------------------------------------------------------------
# Общая база ORM и имя схемы
# -----------------------------------------------------------------------------
Base = declarative_base()

# Имя схемы берём из конфигурации (по умолчанию 'efhc_core')
settings = get_settings()
SCHEMA = getattr(settings, "DB_SCHEMA_CORE", "efhc_core")


# =============================================================================
# Пользователи и связанные сущности
# =============================================================================
class User(Base):
    """
    Пользователь платформы (Telegram-пользователь).

    ВАЖНО: мы используем Telegram ID как первичный ключ (PK). Это упрощает
    обращение во всех ручках (в проекте мы повсюду оперируем telegram_id).
    """
    __tablename__ = "users"
    __table_args__ = (
        {"schema": SCHEMA},
    )

    telegram_id = Column(BigInteger, primary_key=True)  # PK = Telegram ID
    username = Column(String(64), nullable=True)
    first_name = Column(String(128), nullable=True)
    last_name = Column(String(128), nullable=True)
    language_code = Column(String(10), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    balance = relationship("Balance", back_populates="user", uselist=False)
    panels = relationship("Panel", back_populates="user")
    vip_status = relationship("VipStatus", back_populates="user", uselist=False)
    wallets = relationship("TonWallet", back_populates="user")


class TonWallet(Base):
    """
    TON-кошельки пользователя.
      - Храним несколько адресов (при желании), но обычно один — current=TRUE.
      - 'verified' — может использоваться при ручной проверке владения кошельком.
    """
    __tablename__ = "ton_wallets"
    __table_args__ = (
        UniqueConstraint("telegram_id", "address", name="uq_ton_wallets_user_address"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)
    address = Column(Text, nullable=False)
    current = Column(Boolean, nullable=False, default=True)
    verified = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="wallets")


class VipStatus(Base):
    """
    Статус VIP (получается/теряется только по результатам проверки NFT в кошельке).
    Наличие записи с has_nft=True означает, что пользователь VIP (ставка kWh выше).

    ВАЖНО: покупка VIP NFT в магазине НЕ включает VIP автоматически. После оплаты создаётся
    manual-заявка на выдачу NFT, а реальный статус VIP появится только после
    очередной проверки кошелька (ежедневно в 00:00, как описано в ТЗ).
    """
    __tablename__ = "vip_status"
    __table_args__ = (
        {"schema": SCHEMA},
    )

    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), primary_key=True)
    has_nft = Column(Boolean, nullable=False, default=False)         # True → VIP-ставка
    source = Column(String(32), nullable=False, default="nft_presence")  # для расширения (например, promo)
    since = Column(DateTime(timezone=True), nullable=True)           # когда впервые стал VIP
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="vip_status")


class Balance(Base):
    """
    Баланс пользователя:
      • efhc — текущий баланс EFHC (внутренняя монета, 8 знаков).
      • bonus_efhc — бонусные EFHC (тратятся только на панели).
         - Бонусные EFHC начисляются пользователю (user.balance.bonus_efhc += X).
         - При покупке панелей они списываются у пользователя,
           и возвращаются в основной счёт банка EFHC (AdminBankConfig.current_bank_telegram_id).
         - После этого цикл бонуса для пользователя завершается до нового начисления.
         - Общего «бонусного счёта» не существует — бонусы есть только у пользователей, а при трате
           уходят на банк (как обычные EFHC).
      • kwh — доступные «игровые» kWh (8 знаков), растут «лениво» (lazy accrual).
      • last_generated_at — момент последней фиксации роста kWh для ленивой генерации.
    """
    __tablename__ = "balances"
    __table_args__ = (
        {"schema": SCHEMA},
    )

    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), primary_key=True)
    efhc = Column(Numeric(30, 8), nullable=False, default=0)
    bonus_efhc = Column(Numeric(30, 8), nullable=False, default=0)
    kwh = Column(Numeric(30, 8), nullable=False, default=0)
    last_generated_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="balance")


# =============================================================================
# Панели и архив
# =============================================================================
class Panel(Base):
    """
    Солнечная панель (активная). Генерация начинается с момента покупки (activated_at),
    срок действия фиксирован — 180 дней (expires_at).

    В проекте действует ограничение: на одного пользователя одновременно ≤ 1000 активных панелей.
    Это ограничение проверяется на уровне бизнес-логики (shop_routes/admin_routes), а также рекомендуется
    контролировать в БД (например, триггер/проверка), но здесь оставляем на уровне приложений.

    При истечении срока панель переносится в архив (PanelArchive), а здесь помечается inactive либо удаляется.
    """
    __tablename__ = "panels"
    __table_args__ = (
        Index("ix_panels_user_active", "telegram_id", "active"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    activated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False)  # = activated_at + 180 days (рассчитывается в коде)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    user = relationship("User", back_populates="panels")


class PanelArchive(Base):
    """
    Архив панелей. Сюда переносим панели по истечению срока (180 дней).
    Храним основные параметры: период жизни и кто владел.
    """
    __tablename__ = "panel_archive"
    __table_args__ = (
        Index("ix_panel_archive_user", "telegram_id"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="SET NULL"), nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=False)
    expired_at = Column(DateTime(timezone=True), nullable=False)
    archived_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


# =============================================================================
# EFHC переводы/логи (все движения EFHC через эту таблицу)
# =============================================================================
class EFHCTransfersLog(Base):
    """
    Лог всех переводов EFHC (через банк, между пользователями, пополнения, списания):
      • from_id / to_id — Telegram ID отправителя/получателя (банк тоже Telegram ID).
      • amount — NUMERIC(30,8)
      • reason — семантический код операции, например:
          - 'shop_buy_efhc'         — покупка EFHC (банк -> user)
          - 'shop_panel_bonus'      — списание бонусных EFHC: user.bonus -> банк (логируем для аудита)
          - 'shop_panel_efhc'       — списание обычных EFHC: user -> банк
          - 'withdraw_lock'         — блокировка EFHC при создании вывода: user -> банк
          - 'withdraw_refund'       — возврат при отклонении: банк -> user
          - 'airdrop_bonus'         — начисление бонусов (пример)
          - ...
      • idempotency_key — для защиты от двойной записи (опционально).
      • meta — произвольный JSON (например, order_id, withdraw_id).
    """
    __tablename__ = "efhc_transfers_log"
    __table_args__ = (
        Index("ix_efhc_log_from_id", "from_id"),
        Index("ix_efhc_log_to_id", "to_id"),
        UniqueConstraint("idempotency_key", name="uq_efhc_log_idem"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    from_id = Column(BigInteger, nullable=False)
    to_id = Column(BigInteger, nullable=False)
    amount = Column(Numeric(30, 8), nullable=False)
    reason = Column(String(64), nullable=False)
    idempotency_key = Column(String(128), nullable=True)
    meta = Column(JSONB, nullable=True)
    ts = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


# =============================================================================
# Shop: заказы и manual-заявки на NFT
# =============================================================================
class ShopOrder(Base):
    """
    Заказ в магазине:
      - order_type: 'efhc'|'vip'|'nft'
      - efhc_amount: для 'efhc' — сколько EFHC купить
      - pay_asset: 'TON'|'USDT' — чем платил пользователь
      - pay_amount: сколько заплатил (для аналитики)
      - ton_address: адрес плательщика/получателя (отображение)
      - status: 'pending'|'paid'|'completed'|'rejected'|'canceled'|'failed'
      - idempotency_key: защита от дублей
      - tx_hash: хэш внешней оплаты (если есть)
    """
    __tablename__ = "shop_orders"
    __table_args__ = (
        Index("ix_shop_orders_user", "telegram_id"),
        Index("ix_shop_orders_status", "status"),
        UniqueConstraint("idempotency_key", name="uq_shop_orders_idem"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)
    order_type = Column(String(16), nullable=False)                 # 'efhc', 'vip', 'nft'
    efhc_amount = Column(Numeric(30, 8), nullable=True)
    pay_asset = Column(String(16), nullable=True)                   # 'TON'|'USDT'
    pay_amount = Column(Numeric(30, 8), nullable=True)
    ton_address = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="pending")  # см. выше перечисление
    idempotency_key = Column(String(128), nullable=True)
    tx_hash = Column(Text, nullable=True)
    admin_id = Column(BigInteger, nullable=True)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    paid_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class ManualNFTRequest(Base):
    """
    Manual-заявка на выдачу VIP NFT после покупки 'nft' в магазине.
      - request_type = 'vip_nft' (на текущий момент).
      - status: 'open'|'processed'|'canceled'
    """
    __tablename__ = "manual_nft_requests"
    __table_args__ = (
        Index("ix_manual_nft_requests_user", "telegram_id"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)
    wallet_address = Column(Text, nullable=True)
    request_type = Column(String(32), nullable=False, default="vip_nft")
    order_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.shop_orders.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(16), nullable=False, default="open")  # 'open'|'processed'|'canceled'
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


# =============================================================================
# Withdraw: заявки на вывод EFHC
# =============================================================================
class WithdrawRequest(Base):
    """
    Заявка на вывод EFHC:
      • При создании заявки EFHC списываются user → банк (блокируются) — reason='withdraw_lock'.
      • Статусы:
          - 'pending'   — пользователь создал, ждём одобрения админом
          - 'approved'  — админ одобрил
          - 'rejected'  — админ отклонил (нужно вернуть EFHC user → reason='withdraw_refund')
          - 'sent'      — выплата отправлена (tx_hash заполнен)
          - 'failed'    — ошибка при отправке
          - 'canceled'  — отмена
    """
    __tablename__ = "withdrawals"
    __table_args__ = (
        Index("ix_withdrawals_user", "telegram_id"),
        Index("ix_withdrawals_status", "status"),
        UniqueConstraint("idempotency_key", name="uq_withdrawals_idem"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)
    ton_address = Column(Text, nullable=False)
    amount_efhc = Column(Numeric(30, 8), nullable=False)
    asset = Column(String(16), nullable=False, default="TON")  # 'TON'|'USDT' — способ реальной выплаты
    status = Column(String(16), nullable=False, default="pending")
    idempotency_key = Column(String(128), nullable=True)
    tx_hash = Column(Text, nullable=True)
    admin_id = Column(BigInteger, nullable=True)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


# =============================================================================
# Exchange: обменник kWh → EFHC (без обратной операции)
# =============================================================================
class KwhToEfhcExchangeLog(Base):
    """
    Лог операций обмена kWh → EFHC (без обратной операции):
      • user списал amount_kwh → получил amount_efhc (1:1).
      • idempotency_key — ключ идемпотентности (если нужен).
      • reason: 'exchange_kwh_to_efhc' (для единообразия логов EFHCTransfersLog).
      • Здесь же можно хранить 'snapshot_kwh_before'/'after' в meta EFHCTransfersLog, но
        выделяем отдельную таблицу для удобства аналитики.
    """
    __tablename__ = "kwh_to_efhc_exchange_log"
    __table_args__ = (
        Index("ix_exchange_user", "telegram_id"),
        UniqueConstraint("idempotency_key", name="uq_kwh_to_efhc_idem"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)
    amount_kwh = Column(Numeric(30, 8), nullable=False)
    amount_efhc = Column(Numeric(30, 8), nullable=False)
    idempotency_key = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


# =============================================================================
# Рефералы: связи и бонусы
# =============================================================================
class Referral(Base):
    """
    Реферальная связь: кто кого пригласил.
      • invitee_id — приглашённый (уникален; один приглашённый не может иметь 2-х инвайтеров).
      • inviter_id — пригласивший.
    """
    __tablename__ = "referrals"
    __table_args__ = (
        UniqueConstraint("invitee_id", name="uq_referrals_invitee"),
        Index("ix_referrals_inviter", "inviter_id"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    inviter_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="SET NULL"), nullable=True)
    invitee_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class ReferralBonusLog(Base):
    """
    Лог начислений реферальных бонусов:
      • type:
          - 'first_panel' — одноразовый бонус 0.1 EFHC за каждого реферала, купившего 1-ю панель.
          - 'threshold'   — бонусы за достижение порогов активных рефералов:
                           10 → 1 EFHC, 100 → 10 EFHC, 1000 → 100 EFHC, 3000 → 300 EFHC, 10000 → 1000 EFHC.
      • count_at_moment — численность активных рефералов на момент выдачи порогового бонуса.
      • amount_bonus_efhc — NUMERIC(30,8), начисляется в bonus_efhc
      • idempotency_key — защита от дублей.
    """
    __tablename__ = "referral_bonus_log"
    __table_args__ = (
        Index("ix_ref_bonus_user", "telegram_id"),
        UniqueConstraint("idempotency_key", name="uq_ref_bonus_idem"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)
    bonus_type = Column(String(32), nullable=False)  # 'first_panel'|'threshold'
    count_at_moment = Column(Integer, nullable=True)
    amount_bonus_efhc = Column(Numeric(30, 8), nullable=False)
    meta = Column(JSONB, nullable=True)
    idempotency_key = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


# =============================================================================
# Админ-конфигурации: банк и ставки генерации
# =============================================================================
class AdminBankConfig(Base):
    """
    Текущая конфигурация банк-счёта EFHC (админский Telegram ID), на который
    уходят все списания (включая возврат бонусных EFHC при покупке панелей,
    списание при выводе и пр.).

    • Возможна миграция на новый Telegram ID — для этого меняется поле current_bank_telegram_id.
    • Историю изменений фиксируем в AdminBankHistory.
    """
    __tablename__ = "admin_bank_config"
    __table_args__ = (
        {"schema": SCHEMA},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)  # обычно 1 запись
    current_bank_telegram_id = Column(BigInteger, nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class AdminBankHistory(Base):
    """
    История изменения банк-счёта (админского Telegram ID).
      • Можно использовать для аудита и ретроспективного анализа логов EFHC.
    """
    __tablename__ = "admin_bank_history"
    __table_args__ = (
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    old_bank_telegram_id = Column(BigInteger, nullable=False)
    new_bank_telegram_id = Column(BigInteger, nullable=False)
    changed_by_admin = Column(BigInteger, nullable=True)  # кто изменил (админ)
    changed_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class AdminRateChange(Base):
    """
    История изменения базовой и VIP ставок генерации kWh/сутки:
      • base_rate_kwh_per_day (например, 0.59800000)
      • vip_rate_kwh_per_day  (например, 0.64000000)
      • effective_from — когда вступает в силу (согласно бизнес-правилу: в 01:00 следующего дня).
      • changed_by_admin — кто внёс изменение.

    Актуальная ставка определяется как последняя запись с effective_from <= NOW().
    """
    __tablename__ = "admin_rate_change"
    __table_args__ = (
        Index("ix_rate_effective_from", "effective_from"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    base_rate_kwh_per_day = Column(Numeric(12, 8), nullable=False)  # 0.59800000 по умолчанию
    vip_rate_kwh_per_day = Column(Numeric(12, 8), nullable=False)   # 0.64000000 по умолчанию
    effective_from = Column(DateTime(timezone=True), nullable=False)
    changed_by_admin = Column(BigInteger, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


# =============================================================================
# Дополнительные индексы/представления (если нужно)
# =============================================================================
# Пример: можно добавить MATERIALIZED VIEW для лидеров рейтинга по kWh/рефералам,
# но в рамках models.py мы не создаём представления. Их лучше держать в Alembic-миграциях.

# =============================================================================
# ВАЖНО:
# • Инициализация схемы/таблиц выполняется в Alembic миграциях. На старте можно
#   использовать on_startup_init_db() для CREATE SCHEMA IF NOT EXISTS.
# • Все операции начислений/списаний EFHC — через efhc_transactions.py:
#     - debit_user_to_bank(db, user_id, amount)
#     - credit_user_from_bank(db, user_id, amount)
#   Они обновляют Balance + пишут EFHCTransfersLog (idempotent при наличии ключа).
# • Бонусные EFHC тратятся только на панели. При покупке панелей:
#     - bonus списываем у user → зачисляем в Банк (и лог reason='shop_panel_bonus').
#     - недостающую часть — efhc списываем user → Банк (и лог reason='shop_panel_efhc').
# • VIP-статус включается/выключается исключительно по результатам ежедневной NFT-проверки.
#   Покупка VIP NFT в Shop создаёт manual-заявку, не включает VIP автоматически.
# • Генерация kWh стартует сразу с момента покупки первой панели (shop_routes: last_generated_at=NOW()).
# • Ограничение активных панелей на пользователя — валидируется в ручках Shop/Admin (<=1000 активных).
# • EFHC/kWh — строго NUMERIC(30,8) с округлением до 8 знаков в коде (Decimal quantize '0.00000001').
# -----------------------------------------------------------------------------
