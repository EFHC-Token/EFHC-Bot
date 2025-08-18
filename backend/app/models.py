# 📂 backend/app/models.py — SQLAlchemy-модели (полный состав)
# -----------------------------------------------------------------------------
# Что делает:
#   - Описывает все таблицы проекта (ядро, рефералка, лотереи, задания, админ-доступ).
#   - Совместимо с async SQLAlchemy 2.x.
#
# Как связано:
#   - schemas.py использует модели для Pydantic-схем.
#   - сервисы/роуты работают с этими моделями.
#
# Важно:
#   - Денежные поля: Numeric(..., 3) и Decimal в коде.
#   - Не используем ondelete каскады повсеместно — только там, где безопасно.
# -----------------------------------------------------------------------------

from sqlalchemy.orm import declarative_base, relationship, Mapped, mapped_column
from sqlalchemy import (
    BigInteger, Integer, String, Boolean, Numeric, TIMESTAMP, ForeignKey, JSON, UniqueConstraint, Index
)
from sqlalchemy.sql import func

Base = declarative_base()

# -----------------------
# Пользователи / ядро
# -----------------------
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lang: Mapped[str] = mapped_column(String(8), default="RU")

    wallet_ton: Mapped[str | None] = mapped_column(String(128), unique=False, nullable=True)
    # Основной баланс EFHC — выводимый/торговый
    main_balance: Mapped[str] = mapped_column(Numeric(14, 3), default=0, nullable=False)
    # Бонусный EFHC — внутренний, невылазной (тратится на панели/апгрейды)
    bonus_balance: Mapped[str] = mapped_column(Numeric(14, 3), default=0, nullable=False)

    total_generated_kwh: Mapped[str] = mapped_column(Numeric(18, 3), default=0, nullable=False)
    todays_generated_kwh: Mapped[str] = mapped_column(Numeric(18, 3), default=0, nullable=False)

    is_active_user: Mapped[bool] = mapped_column(Boolean, default=False)   # активный после 1-й панели
    has_vip: Mapped[bool] = mapped_column(Boolean, default=False)          # кэш флага VIP по NFT-проверке

    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    panels: Mapped[list["Panel"]] = relationship("Panel", back_populates="owner")

Index("ix_users_referred_by", User.referred_by)

class Panel(Base):
    __tablename__ = "panels"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purchase_date: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    lifespan_days: Mapped[int] = mapped_column(Integer, default=180)
    daily_generation: Mapped[str] = mapped_column(Numeric(10, 3), default=0.598)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    owner: Mapped["User"] = relationship("User", back_populates="panels")

class TransactionLog(Base):
    __tablename__ = "transaction_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    op_type: Mapped[str] = mapped_column(String(64))     # buy_panel, bonus_award, main_transfer, exchange и т.д.
    amount: Mapped[str] = mapped_column(Numeric(14, 3))
    source: Mapped[str] = mapped_column(String(32))      # bonus, main, combined, kwh, referral, admin
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

Index("ix_tx_user_op", TransactionLog.user_id, TransactionLog.op_type)

# -----------------------
# Реферальная система
# -----------------------
class Referral(Base):
    __tablename__ = "referrals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    invited_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)  # один раз
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)               # становится True после 1-й панели
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

Index("ix_ref_inviter_active", Referral.inviter_id, Referral.is_active)

class ReferralMilestone(Base):
    __tablename__ = "referral_milestones"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    inviter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    milestone: Mapped[int] = mapped_column(Integer)     # 10, 100, 1000...
    reward_efhc: Mapped[str] = mapped_column(Numeric(14, 3))
    awarded: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

UniqueConstraint("inviter_id", "milestone", name="uq_ref_milestone_once")

# -----------------------
# Лотереи (розыгрыши)
# -----------------------
class Lottery(Base):
    __tablename__ = "lotteries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)       # например "lottery_vip_2025_08_01"
    title: Mapped[str] = mapped_column(String(128))
    prize_type: Mapped[str] = mapped_column(String(32))              # VIP_NFT | PANEL
    target_participants: Mapped[int] = mapped_column(Integer)        # 500 или 200
    ticket_price_efhc: Mapped[str] = mapped_column(Numeric(14, 3), default=1)
    max_tickets_per_user: Mapped[int] = mapped_column(Integer, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    ended_at: Mapped[str | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    winner_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

class LotteryTicket(Base):
    __tablename__ = "lottery_tickets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lottery_id: Mapped[int] = mapped_column(ForeignKey("lotteries.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    ticket_number: Mapped[int] = mapped_column(Integer)  # порядковый №
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

UniqueConstraint("lottery_id", "user_id", "ticket_number", name="uq_lottery_user_ticket")

# -----------------------
# Задания (Tasks)
# -----------------------
class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # тип: subscribe / visit / like / repost / promo / poll
    type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(256))
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Сколько выполнений доступно (лимит рекламодателя)
    available_count: Mapped[int] = mapped_column(Integer, default=0)
    reward_bonus_efhc: Mapped[str] = mapped_column(Numeric(14, 3), default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)  # активируется после оплаты
    created_by_admin_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

class TaskCompletion(Base):
    __tablename__ = "task_completions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    status: Mapped[str] = mapped_column(String(32), default="done")  # done / rejected
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

UniqueConstraint("task_id", "user_id", name="uq_task_once_per_user")

# -----------------------
# Админская часть / права
# -----------------------
class AdminNFT(Base):
    __tablename__ = "admin_nft_whitelist"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Ссылка/идентификатор NFT из вашей коллекции, дающей доступ в админку
    nft_url: Mapped[str] = mapped_column(String(512), unique=True)
    # Могут ли владельцы этого NFT заходить в админку
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

class AdminNFTPermission(Base):
    __tablename__ = "admin_nft_permissions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    admin_nft_id: Mapped[int] = mapped_column(ForeignKey("admin_nft_whitelist.id", ondelete="CASCADE"), index=True)
    # Гранулярные права (галочками): shop / tasks / lotteries / users / withdrawals / panels / everything
    can_shop: Mapped[bool] = mapped_column(Boolean, default=False)
    can_tasks: Mapped[bool] = mapped_column(Boolean, default=False)
    can_lotteries: Mapped[bool] = mapped_column(Boolean, default=False)
    can_users: Mapped[bool] = mapped_column(Boolean, default=False)
    can_withdrawals: Mapped[bool] = mapped_column(Boolean, default=False)
    can_panels: Mapped[bool] = mapped_column(Boolean, default=False)
    can_all: Mapped[bool] = mapped_column(Boolean, default=False)

# -----------------------
# Магазин / заказы / заявки
# -----------------------
class ShopItem(Base):
    __tablename__ = "shop_items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)   # efhc_100_usdt, vip_ton и т.д.
    label: Mapped[str] = mapped_column(String(128))
    pay_asset: Mapped[str] = mapped_column(String(16))          # TON | USDT | EFHC
    price: Mapped[str] = mapped_column(Numeric(14, 3))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

class ShopOrder(Base):
    __tablename__ = "shop_orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    item_code: Mapped[str] = mapped_column(String(64), index=True)
    pay_asset: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/paid/canceled/failed
    # Мемо ID для входящих (TON/USDT): Telegram ID пользователя
    memo_telegram_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tx_hash: Mapped[str | None] = mapped_column(String(256), nullable=True)  # хеш внешней транзакции (если есть)
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

# -----------------------
# Заявки на вывод EFHC и заявки на выдачу VIP NFT (вручную)
# -----------------------
class WithdrawalRequest(Base):
    __tablename__ = "withdrawal_requests"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    amount_efhc: Mapped[str] = mapped_column(Numeric(14, 3))
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/approved/rejected/paid
    history: Mapped[list | None] = mapped_column(JSON, nullable=True)   # история статусов
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

class VipNftRequest(Base):
    __tablename__ = "vip_nft_requests"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    pay_asset: Mapped[str] = mapped_column(String(16))          # EFHC/TON/USDT
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/approved/rejected/sent
    history: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Для ручной отправки NFT админом (через Tonkeeper): после оплаты заявка получает статус done/sent
    created_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())
    updated_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

# -----------------------
# Кэш владения VIP NFT (для ускорения ежедневной проверки)
# -----------------------
class VipOwnershipCache(Base):
    __tablename__ = "vip_ownership_cache"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    has_vip: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked_at: Mapped[str] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())
