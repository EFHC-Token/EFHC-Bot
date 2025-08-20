# 📂 backend/app/models.py — SQLAlchemy ORM-модели проекта EFHC (ПОЛНАЯ ВЕРСИЯ)
# -----------------------------------------------------------------------------
# Назначение:
#   • Описывает все основные таблицы EFHC-экосистемы:
#       - Пользователи, балансы, кошельки.
#       - Панели и их жизненный цикл (180 дней), архив в той же таблице (active=false).
#       - VIP-статус (по результатам ежедневной проверки NFT в TON-кошельке).
#       - EFHC-транзакции (через Банк) и лог переводов/списаний.
#       - Магазин (Shop): заказы EFHC/VIP/NFT и заявки на ручную выдачу NFT.
#       - Выводы EFHC: блокировка EFHC (user→Банк), approve/reject/send.
#       - Реферальные связи и логи бонусов.
#       - Дневная агрегированная статистика генерации (для истории/аналитики).
#
# Важные бизнес-правила (закрепляем в моделях и комментариях):
#   • EFHC и kWh храним в NUMERIC(30,8).
#   • 1 EFHC = 1 kWh (внутренняя эквивалентность).
#   • Генерация kWh — «ленивая»:
#       - В модели User есть поле last_generated_at.
#       - В модели Balance есть поля kwh_available (что можно обменять) и kwh_total (жизненный счётчик для рейтинга).
#       - При любой активности (вход в WebApp / обмен / покупка / просмотр профиля) бекенд обновляет генерацию:
#           delta_seconds = now - last_generated_at
#           added = rate * active_panels * delta_seconds
#           kwh_available += added; kwh_total += added; last_generated_at = now
#     NFT-проверка (ежедневно) только переключает ставку для будущих начислений (VIP=0.640/сутки или 0.598/сутки).
#   • Панели: срок всегда 180 дней (expires_at = activated_at + interval '180 days').
#     Архив — те же записи с active=false и archived_at != NULL.
#   • Все EFHC-движения только через Банк (ID=362746228), лог в efhc_transfers_log.
#   • Бонусные EFHC — начисляются из Банка (bonus) и тратятся строго на панели, при покупке панелей возвращаются Банку.
#   • WebApp разделы: Обменник (обмен kWh→EFHC), Panels (управление панелями), Рейтинг (лидерборды),
#     Shop (покупка EFHC/VIP/NFT), Withdraw (вывод EFHC).
#   • Админ-панель: банк EFHC, модерация Shop/Withdraw, лимиты, прайс-листы, просмотр логов.
#
# Совместимость, версии и соглашения:
#   • SQLAlchemy v2.x (асинхронная работа в database.py).
#   • FastAPI 0.103.2 (Pydantic v1).
#   • ВСЕ денежные/энергетические поля: Numeric(30,8) — округление в коде (Decimal quantize, 8 знаков).
#   • Схема по умолчанию: settings.DB_SCHEMA_CORE (например, "efhc_core").
#
# Где используется:
#   • Во всех роутерах (user/admin/shop/withdraw/panels/exchange/rating).
#   • В планировщике (scheduler.py) — архивирование панелей и VIP-сканер.
#   • В транзакциях (efhc_transactions.py) — списания/зачисления EFHC.
# -----------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    JSON,
    func,
)
from sqlalchemy.orm import declarative_base, relationship

from .config import get_settings

# -----------------------------------------------------------------------------
# Конфигурация, базовые объекты и соглашения имён
# -----------------------------------------------------------------------------
settings = get_settings()

# Единые имена ограничений для Alembic (рекомендуемая практика)
# Это помогает кросс-БД миграциям и корректному autogenerate.
naming_convention = {
    "ix": "ix_%(schema)s_%(table_name)s_%(column_0_label)s",
    "uq": "uq_%(schema)s_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(schema)s_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(schema)s_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(schema)s_%(table_name)s",
}
metadata = MetaData(naming_convention=naming_convention)
Base = declarative_base(metadata=metadata)

SCHEMA = settings.DB_SCHEMA_CORE  # например, 'efhc_core'

# -----------------------------------------------------------------------------
# Модель: User — базовый профиль пользователя
# -----------------------------------------------------------------------------
class User(Base):
    """
    Пользователь EFHC экосистемы.

    Поля:
      • telegram_id — уникальный идентификатор пользователя в Telegram (используем как PK).
      • username / first_name / last_name — для UI/аналитики.
      • referred_by — реферер (telegram_id), если есть.
      • created_at — дата регистрации в системе.
      • last_generated_at — последний момент, когда мы "лениво" начисляли kWh пользователю.
        Используется, чтобы при любых действиях пересчитать kWh за прошедшее время.
    """
    __tablename__ = "users"
    __table_args__ = (
        {"schema": SCHEMA},
    )

    telegram_id = Column(BigInteger, primary_key=True, autoincrement=False)
    username = Column(String(length=64), nullable=True)
    first_name = Column(String(length=128), nullable=True)
    last_name = Column(String(length=128), nullable=True)

    referred_by = Column(BigInteger, nullable=True)  # на telegram_id другого пользователя
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Ленивый пересчёт генерации kWh: точка отсчёта
    last_generated_at = Column(DateTime(timezone=True), nullable=True)

    # Отношения (не обязательны, но полезны в ORM)
    balance = relationship("Balance", back_populates="user", uselist=False)
    wallets = relationship("UserWallet", back_populates="user", cascade="all, delete-orphan")
    panels = relationship("Panel", back_populates="user")

    def __repr__(self) -> str:
        return f"<User tg={self.telegram_id} username={self.username!r}>"

# Индекс для поиска по рефереру (аналитика)
Index("ix_efhc_core_users_referred_by", User.referred_by, schema=SCHEMA)

# -----------------------------------------------------------------------------
# Модель: Balance — финансовый и энергетический баланс пользователя
# -----------------------------------------------------------------------------
class Balance(Base):
    """
    Балансы пользователя.

    Поля:
      • telegram_id — пользователь (PK, == User.telegram_id).
      • efhc — основной баланс EFHC (может быть пополнен через Shop/обмен kWh→EFHC/и т.д.).
      • bonus — бонусные EFHC (только для покупки панелей).
      • kwh_available — текущая "доступная" энергия (копится «лениво» и уменьшается при обмене в EFHC).
      • kwh_total — суммарно сгенерированная энергия за время жизни (для рейтинга, не уменьшается).
      • updated_at — отметка последнего изменения балансов.
    """
    __tablename__ = "balances"
    __table_args__ = (
        {"schema": SCHEMA},
    )

    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), primary_key=True)

    # Денежные/энергетические поля — строго NUMERIC(30,8)
    efhc = Column(Numeric(30, 8), nullable=False, server_default="0.00000000")
    bonus = Column(Numeric(30, 8), nullable=False, server_default="0.00000000")

    kwh_available = Column(Numeric(30, 8), nullable=False, server_default="0.00000000")
    kwh_total = Column(Numeric(30, 8), nullable=False, server_default="0.00000000")

    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user = relationship("User", back_populates="balance")

    def __repr__(self) -> str:
        return f"<Balance tg={self.telegram_id} efhc={self.efhc} bonus={self.bonus} kwh_avail={self.kwh_available} kwh_total={self.kwh_total}>"

# -----------------------------------------------------------------------------
# Модель: UserVIPStatus — VIP-статус пользователя (по наличию NFT)
# -----------------------------------------------------------------------------
class UserVIPStatus(Base):
    """
    VIP-статус пользователя (активен, если у пользователя есть NFT из коллекции EFHC).
    Поддерживается ежедневной задачей (scheduler: 00:00 — проверка NFT).

    Поля:
      • telegram_id — пользователь (PK).
      • since — с какого момента VIP-статус активен.
      • source — источник статуса: 'nft' (для однозначности).
      • updated_at — сервисное поле.
    """
    __tablename__ = "user_vip_status"
    __table_args__ = (
        {"schema": SCHEMA},
    )

    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), primary_key=True)
    since = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    source = Column(String(length=16), nullable=False, server_default="nft")
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<UserVIPStatus tg={self.telegram_id} since={self.since} source={self.source}>"

# -----------------------------------------------------------------------------
# Модель: UserWallet — кошельки пользователя (TON и др.)
# -----------------------------------------------------------------------------
class UserWallet(Base):
    """
    Пользовательские крипто-кошельки (основной сценарий — TON).
    Хранится несколько адресов на пользователя; для выплат используется is_primary.

    Поля:
      • id — PK.
      • telegram_id — пользователь.
      • chain — 'TON', на будущее можно расширить.
      • address — адрес в соответствующей сети, уникален в пределах chain.
      • is_primary — основной адрес для выплат/доставки NFT.
      • created_at / updated_at
    """
    __tablename__ = "user_wallets"
    __table_args__ = (
        UniqueConstraint("chain", "address", name=f"uq_{SCHEMA}_user_wallets_chain_address"),
        {"schema": SCHEMA},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)

    chain = Column(String(length=16), nullable=False, server_default="TON")  # 'TON'
    address = Column(Text, nullable=False)
    is_primary = Column(Boolean, nullable=False, server_default="false")

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    user = relationship("User", back_populates="wallets")

    def __repr__(self) -> str:
        return f"<UserWallet id={self.id} tg={self.telegram_id} chain={self.chain} primary={self.is_primary}>"

Index("ix_efhc_core_user_wallets_tg", UserWallet.telegram_id, schema=SCHEMA)
Index("ix_efhc_core_user_wallets_chain", UserWallet.chain, schema=SCHEMA)

# -----------------------------------------------------------------------------
# Модель: Panel — запись о панели пользователя (180 дней, архив внутри таблицы)
# -----------------------------------------------------------------------------
class Panel(Base):
    """
    Панель пользователя.

    Поля:
      • id — PK.
      • telegram_id — владелец панели.
      • active — активна ли панель в данный момент.
      • activated_at — дата активации (момент покупки).
      • expires_at — дата автоматического завершения (activated_at + 180 дней).
      • archived_at — когда мы перевели панель в архив (active=false).
    Правила:
      • Кол-во активных панелей у пользователя ограничено (<= 1000).
      • Панели дают генерацию по ставке пользователя (VIP/обычный) *число активных панелей.
    """
    __tablename__ = "panels"
    __table_args__ = (
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)

    active = Column(Boolean, nullable=False, server_default="true")
    activated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)  # выставляется при создании = activated_at + 180 days
    archived_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="panels")

    def __repr__(self) -> str:
        return f"<Panel id={self.id} tg={self.telegram_id} active={self.active}>"


Index("ix_efhc_core_panels_tg_active", Panel.telegram_id, Panel.active, schema=SCHEMA)

# -----------------------------------------------------------------------------
# Модель: EFHCTransfersLog — журнал переводов EFHC (включая бонусный поток)
# -----------------------------------------------------------------------------
class EFHCTransfersLog(Base):
    """
    Журнал EFHC-переводов (вкл. бонусные EFHC как отдельный reason).
    Все движения EFHC должны логироваться:
      • from_id -> to_id, amount
      • reason (например: 'shop_panel_purchase', 'withdraw_lock', 'withdraw_refund', 'shop_panel_bonus')
      • idempotency_key — для защиты от дублей при повторной отправке.
      • details — JSON для произвольных метаданных (context).
      • ts — метка времени.

    ВАЖНО: это логовая таблица, не источник правды. Истина — остатки в balances.
    """
    __tablename__ = "efhc_transfers_log"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name=f"uq_{SCHEMA}_efhc_transfers_log_idempotency_key"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    from_id = Column(BigInteger, nullable=False)  # telegram_id отправителя (или BANK_TELEGRAM_ID)
    to_id = Column(BigInteger, nullable=False)    # telegram_id получателя (или BANK_TELEGRAM_ID)
    amount = Column(Numeric(30, 8), nullable=False)
    reason = Column(String(length=64), nullable=False)
    idempotency_key = Column(String(length=128), nullable=True)

    details = Column(JSON, nullable=True)
    ts = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<EFHCTransferLog id={self.id} {self.from_id}->{self.to_id} amt={self.amount} reason={self.reason}>"

Index("ix_efhc_core_efhc_transfers_log_from", EFHCTransfersLog.from_id, schema=SCHEMA)
Index("ix_efhc_core_efhc_transfers_log_to", EFHCTransfersLog.to_id, schema=SCHEMA)
Index("ix_efhc_core_efhc_transfers_log_reason", EFHCTransfersLog.reason, schema=SCHEMA)

# -----------------------------------------------------------------------------
# Модель: ShopOrder — заказы магазина (EFHC/VIP/NFT)
# -----------------------------------------------------------------------------
class ShopOrder(Base):
    """
    Заказ в магазине (Shop):
      • order_type: 'efhc' | 'vip' | 'nft'
      • Статусы: 'pending','paid','completed','rejected','canceled','failed'
      • efhc_amount — актуально для 'efhc'
      • pay_asset/pay_amount/ton_address — от внешней оплаты (TON/USDT)
      • idempotency_key — для защиты от дублей
      • tx_hash — внешняя транзакция (оплата)
    """
    __tablename__ = "shop_orders"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name=f"uq_{SCHEMA}_shop_orders_idempotency_key"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)

    order_type = Column(String(length=16), nullable=False)  # 'efhc'|'vip'|'nft'
    efhc_amount = Column(Numeric(30, 8), nullable=True)

    pay_asset = Column(String(length=16), nullable=True)   # 'TON'|'USDT'
    pay_amount = Column(Numeric(30, 8), nullable=True)
    ton_address = Column(Text, nullable=True)

    status = Column(String(length=16), nullable=False, server_default="pending")
    idempotency_key = Column(String(length=128), nullable=True)
    tx_hash = Column(Text, nullable=True)

    admin_id = Column(BigInteger, nullable=True)
    comment = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    paid_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<ShopOrder id={self.id} tg={self.telegram_id} type={self.order_type} status={self.status}>"

Index("ix_efhc_core_shop_orders_tg", ShopOrder.telegram_id, schema=SCHEMA)
Index("ix_efhc_core_shop_orders_status", ShopOrder.status, schema=SCHEMA)
Index("ix_efhc_core_shop_orders_type", ShopOrder.order_type, schema=SCHEMA)

# -----------------------------------------------------------------------------
# Модель: ManualNFTRequest — заявка на ручную выдачу VIP NFT
# -----------------------------------------------------------------------------
class ManualNFTRequest(Base):
    """
    Ручная заявка на выдачу NFT (например, после покупки VIP NFT в Shop):
      • request_type — 'vip_nft'
      • status — 'open','processed','canceled'
    """
    __tablename__ = "manual_nft_requests"
    __table_args__ = (
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)

    wallet_address = Column(Text, nullable=True)
    request_type = Column(String(length=32), nullable=False, server_default="vip_nft")
    order_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.shop_orders.id", ondelete="SET NULL"), nullable=True)

    status = Column(String(length=16), nullable=False, server_default="open")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<ManualNFTRequest id={self.id} tg={self.telegram_id} type={self.request_type} status={self.status}>"

Index("ix_efhc_core_manual_nft_requests_tg", ManualNFTRequest.telegram_id, schema=SCHEMA)
Index("ix_efhc_core_manual_nft_requests_status", ManualNFTRequest.status, schema=SCHEMA)

# -----------------------------------------------------------------------------
# Модель: Withdrawal — заявки на вывод EFHC
# -----------------------------------------------------------------------------
class Withdrawal(Base):
    """
    Заявка на вывод EFHC:
      • При создании: списываем EFHC user → Банк (блокировка средств), status='pending'.
      • Админ-операции: approve -> send (manual/webhook) -> sent | failed | rejected.
      • idempotency_key — уникальность заявки.
      • tx_hash — внешняя транзакция выплаты.
    """
    __tablename__ = "withdrawals"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name=f"uq_{SCHEMA}_withdrawals_idempotency_key"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)

    ton_address = Column(Text, nullable=False)
    amount_efhc = Column(Numeric(30, 8), nullable=False)
    asset = Column(String(length=16), nullable=False, server_default="TON")  # 'TON' | 'USDT'

    status = Column(
        String(length=16),
        nullable=False,
        server_default="pending"
    )  # 'pending','approved','rejected','sent','failed','canceled'

    idempotency_key = Column(String(length=128), nullable=True)
    tx_hash = Column(Text, nullable=True)

    admin_id = Column(BigInteger, nullable=True)
    comment = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    approved_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<Withdrawal id={self.id} tg={self.telegram_id} amt={self.amount_efhc} status={self.status}>"

Index("ix_efhc_core_withdrawals_tg", Withdrawal.telegram_id, schema=SCHEMA)
Index("ix_efhc_core_withdrawals_status", Withdrawal.status, schema=SCHEMA)

# -----------------------------------------------------------------------------
# Модель: ReferralLink — связи рефералов (кто кого пригласил)
# -----------------------------------------------------------------------------
class ReferralLink(Base):
    """
    Реферальные связи:
      • referrer_id — кто пригласил (telegram_id).
      • referee_id — кого пригласили (telegram_id).
      • activated — стал ли реферал «активным» (купил хотя бы одну панель).
      • activated_at — когда стал активным.
      • created_at — дата связки.

    Уникальность пары (referrer_id, referee_id).
    """
    __tablename__ = "referral_links"
    __table_args__ = (
        UniqueConstraint("referrer_id", "referee_id", name=f"uq_{SCHEMA}_referral_links_pair"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    referrer_id = Column(BigInteger, nullable=False)
    referee_id = Column(BigInteger, nullable=False)

    activated = Column(Boolean, nullable=False, server_default="false")
    activated_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<ReferralLink id={self.id} ref={self.referrer_id}->{self.referee_id} active={self.activated}>"

Index("ix_efhc_core_referral_links_referrer", ReferralLink.referrer_id, schema=SCHEMA)
Index("ix_efhc_core_referral_links_referee", ReferralLink.referee_id, schema=SCHEMA)

# -----------------------------------------------------------------------------
# Модель: ReferralBonusLog — лог начислений реферальных бонусов
# -----------------------------------------------------------------------------
class ReferralBonusLog(Base):
    """
    Лог начислений реферальных бонусов:
      • referrer_id — кому начислили (рефереру).
      • referee_id — за какого реферала.
      • amount_efhc — сколько начислено (в EFHC, NUMERIC(30,8)).
      • tier — уровень (например: 'activation', '10', '100', '1000', '3000', '10000').
      • idempotency_key — для защиты от дублей.
      • created_at — когда начислено.
    """
    __tablename__ = "referral_bonus_log"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name=f"uq_{SCHEMA}_referral_bonus_log_idempotency_key"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    referrer_id = Column(BigInteger, nullable=False)
    referee_id = Column(BigInteger, nullable=False)

    amount_efhc = Column(Numeric(30, 8), nullable=False)
    tier = Column(String(length=32), nullable=False)  # 'activation' or milestone level

    idempotency_key = Column(String(length=128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<ReferralBonusLog id={self.id} rf={self.referrer_id} rfe={self.referee_id} amt={self.amount_efhc} tier={self.tier}>"

Index("ix_efhc_core_ref_bonus_log_referrer", ReferralBonusLog.referrer_id, schema=SCHEMA)

# -----------------------------------------------------------------------------
# Модель: DailyKwhLog — дневной агрегированный лог начисления kWh (необязательно для расчётов)
# -----------------------------------------------------------------------------
class DailyKwhLog(Base):
    """
    Дневной агрегированный лог начисления kWh для аналитики/истории.
    НЕ используется для «истины» (истина — balances + last_generated_at).
    Может быть заполнен планировщиком или по итогам дня.

    Поля:
      • id — PK.
      • telegram_id — пользователь.
      • date_utc — дата (UTC) за которую формируется сумма (например, 2025-01-20).
      • kwh_amount — сколько энергии набежало за этот день (NUMERIC(30,8)).
      • created_at — когда записали агрегат.
    """
    __tablename__ = "daily_kwh_log"
    __table_args__ = (
        UniqueConstraint("telegram_id", "date_utc", name=f"uq_{SCHEMA}_daily_kwh_log_user_date"),
        {"schema": SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, ForeignKey(f"{SCHEMA}.users.telegram_id", ondelete="CASCADE"), nullable=False)

    date_utc = Column(DateTime(timezone=True), nullable=False)  # рекомендуется хранить на полночь UTC
    kwh_amount = Column(Numeric(30, 8), nullable=False, server_default="0.00000000")

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<DailyKwhLog id={self.id} tg={self.telegram_id} date={self.date_utc} kwh={self.kwh_amount}>"

Index("ix_efhc_core_daily_kwh_log_tg_date", DailyKwhLog.telegram_id, DailyKwhLog.date_utc, schema=SCHEMA)

# -----------------------------------------------------------------------------
# Примечания по дальнейшей интеграции:
# -----------------------------------------------------------------------------
# • Инициализация схемы/таблиц выполняется в Alembic миграциях (рекомендуется).
#   На старте можно использовать on_startup_init_db() для CREATE SCHEMA IF NOT EXISTS и т.п.
# • Все операции начислений/списаний EFHC — через efhc_transactions.py:
#     - debit_user_to_bank(db, user_id, amount)
#     - credit_user_from_bank(db, user_id, amount)
#   Они обновляют Balance + пишут EFHCTransfersLog (idempotent при наличии ключа).
# • Бонусные EFHC тратятся только на панели. При покупке панелей:
#     - bonus списываем у user → зачисляем в bonus Банка (и лог).
#     - недостающую часть — efhc списываем user → Банк (и лог).
# • VIP-статус включается/выключается исключительно по результатам ежедневной NFT-проверки.
#   Покупка VIP NFT в Shop создаёт manual-заявку, не включает VIP автоматически.
# • Генерация kWh стартует сразу с момента покупки первой панели (shop_routes: last_generated_at=NOW()).
# • Ограничение активных панелей на пользователя — валидируется в ручках Shop/Admin (<=1000 активных).
# -----------------------------------------------------------------------------
