# 📂 backend/app/models.py — ORM-модели EFHC Bot (SQLAlchemy 2.x, async)
# =============================================================================
# Этот модуль описывает ВСЕ таблицы проекта EFHC:
#
# СХЕМЫ (schemas) — вынесены в config.py и подставляются динамически:
#   • efhc_core      — основные сущности: пользователи, балансы, панели, VIP, ежедневные начисления.
#   • efhc_admin     — whitelist NFT для доступа к админке.
#   • efhc_referrals — реферальная система.
#   • efhc_lottery   — розыгрыши и билеты.
#   • efhc_tasks     — задания и прогресс по заданиям.
#
# Стек:
#   • SQLAlchemy ORM (declarative)
#   • Асинхронное подключение через asyncpg (см. database.py)
#   • Миграции — Alembic (рекомендуется, см. структуру проекта)
#
# ВАЖНО:
#   • Все суммы (EFHC, kWh, бонусы) храним в Decimal/NUMERIC — точность задаём из config.py.
#   • Для часто используемых полей (telegram_id, связки lottery_id + telegram_id и т.п.) создаём индексы.
#   • Добавляем audited-поля: created_at/updated_at, где полезно.
#
# Где используется:
#   • services/*.py — бизнес-логика (начисления, покупки, обменник).
#   • routers/*.py  — API-роуты FastAPI.
#   • scheduler.py  — ежедневные начисления энергии, проверки VIP по NFT.
#   • bot.py        — Telegram бот (читает/пишет через сервисы и/или API).
#
# Подсказки по поддержке:
#   • Для новых таблиц — создайте класс здесь и добавьте миграцию Alembic.
#   • Не меняйте precision/scale Numerics «на горячую» — делайте через миграции.
#   • Схемы создаются в database.py → ensure_schemas().
# =============================================================================

from __future__ import annotations

from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Column,
    String,
    Integer,
    DateTime,
    Numeric,
    Boolean,
    ForeignKey,
    Date,
    Text,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .database import Base
from .config import get_settings

# Конфигурация загружается единым инстансом (singleton)
settings = get_settings()

# =============================================================================
# СХЕМА efhc_core — Пользователи, Балансы, Панели, VIP, Ежедневные начисления
# =============================================================================

class User(Base):
    """
    Пользователь бота (ключевая сущность, связываемая по Telegram ID).

    Назначение:
      • Хранит основной профиль пользователя.
      • Связи:
          - 1:1 -> Balance (баланс EFHC/kWh/bonus)
          - 1:N -> Panel (набор панелей)
          - 1:1 -> UserVIP (наличие VIP-доступа)
      • Telegram ID используется как primary key (int).

    Частые операции:
      • get_or_create_user(telegram_id)
      • загрузка профиля, языка, юзернейма
    """
    __tablename__ = "users"
    __table_args__ = (
        # Индекс по username (поиск по нику может пригодиться в админке)
        Index("ix_core_users_username", "username"),
        {"schema": settings.DB_SCHEMA_CORE},
    )

    # Telegram ID — первичный ключ, уникальный и индексируемый
    telegram_id = Column(Integer, primary_key=True, index=True, unique=True)

    # Telegram username (не гарантируется) — может быть None
    username = Column(String, nullable=True)

    # Предпочитаемый язык (по умолчанию — settings.DEFAULT_LANG)
    lang = Column(String(10), default=settings.DEFAULT_LANG)

    # Дата регистрации в системе
    created_at = Column(DateTime, default=datetime.utcnow)

    # ORM-связи:
    balance = relationship("Balance", uselist=False, back_populates="user")
    panels = relationship("Panel", back_populates="user")
    vip = relationship("UserVIP", uselist=False, back_populates="user")

    def __repr__(self) -> str:
        return f"<User telegram_id={self.telegram_id} username={self.username!r}>"


class Balance(Base):
    """
    Баланс пользователя (EFHC, бонусные EFHC, кВт·ч).

    Назначение:
      • Хранит текущие значения валют/ресурсов:
          - efhc  — основной баланс EFHC (для покупок, розыгрышей и т.д.)
          - bonus — бонусные EFHC (копятся от заданий/акций, сначала расходуются при покупке панели)
          - kwh   — накопленные киловатт-часы (обмениваются на EFHC 1:1)
      • Один к одному с User (telegram_id).

    Формат:
      • Decimal/NUMERIC для точных расчётов.
      • Точность берём из настроек (EFHC_DECIMALS, KWH_DECIMALS).
    """
    __tablename__ = "balances"
    __table_args__ = (
        UniqueConstraint("telegram_id", name="uq_core_balances_telegram_id"),
        {"schema": settings.DB_SCHEMA_CORE},
    )

    # Внутренний surrogate primary key
    id = Column(Integer, primary_key=True)

    # Внешний ключ в users.telegram_id — уникальный (1:1 связь)
    telegram_id = Column(
        Integer,
        ForeignKey(f"{settings.DB_SCHEMA_CORE}.users.telegram_id"),
        unique=True,
        index=True,
        nullable=False,
    )

    # Основной EFHC (NUMERIC с заданной точностью)
    efhc = Column(Numeric(20, settings.EFHC_DECIMALS), default=Decimal("0.000"))

    # Бонусные EFHC (расходуются первыми при покупке панели)
    bonus = Column(Numeric(20, settings.EFHC_DECIMALS), default=Decimal("0.000"))

    # Киловатт-часы (накапливаются ежедневно от панелей)
    kwh = Column(Numeric(20, settings.KWH_DECIMALS), default=Decimal("0.000"))

    # ORM-связь обратно к User
    user = relationship("User", back_populates="balance")

    def __repr__(self) -> str:
        return f"<Balance tg={self.telegram_id} efhc={self.efhc} bonus={self.bonus} kwh={self.kwh}>"


class Panel(Base):
    """
    Панель пользователя (может быть несколько, с разной датой покупки/окончания).

    Назначение:
      • Отражает факт покупки "панели" (стоимость: 100 EFHC).
      • Каждый инстанс = купленный пакет, поле count позволяет батч-покупки (если нужно).
      • Срок жизни — 180 дней (PANEL_LIFESPAN_DAYS), после чего панель перестаёт генерировать.

    Поля:
      • telegram_id   — владелец.
      • count         — сколько панелей в одной записи.
      • purchased_at  — дата покупки.
      • expires_at    — дата окончания генерации (может быть precomputed на момент покупки).
    """
    __tablename__ = "panels"
    __table_args__ = (
        # Индекс по (telegram_id, expires_at) — частые выборки активных панелей пользователя
        Index("ix_core_panels_tg_expires", "telegram_id", "expires_at"),
        {"schema": settings.DB_SCHEMA_CORE},
    )

    id = Column(Integer, primary_key=True)

    # Владелец панели
    telegram_id = Column(
        Integer,
        ForeignKey(f"{settings.DB_SCHEMA_CORE}.users.telegram_id"),
        index=True,
        nullable=False,
    )

    # Количество панелей (если покупка пачкой — например, 5 панелей одним действием)
    count = Column(Integer, default=1)

    # Время покупки
    purchased_at = Column(DateTime, default=datetime.utcnow)

    # Время окончания (purchased_at + PANEL_LIFESPAN_DAYS); может быть рассчитано на сервисном уровне
    expires_at = Column(DateTime, nullable=True)

    # ORM-связь с пользователем
    user = relationship("User", back_populates="panels")

    def __repr__(self) -> str:
        return f"<Panel id={self.id} tg={self.telegram_id} count={self.count} expires_at={self.expires_at}>"


class UserVIP(Base):
    """
    Признак VIP доступа у пользователя.

    Назначение:
      • Позволяет увеличивать ежедневную генерацию (множитель VIP_MULTIPLIER = 1.07).
      • Может быть выдан при покупке NFT, вручную, или по whitelist.

    Поля:
      • telegram_id  — кто является VIP.
      • nft_address  — конкретный NFT, на основании которого выдан VIP (для аудита).
      • activated_at — когда выдан VIP.
    """
    __tablename__ = "user_vip"
    __table_args__ = (
        {"schema": settings.DB_SCHEMA_CORE},
    )

    # telegram_id — primary key, 1:1 к users
    telegram_id = Column(
        Integer,
        ForeignKey(f"{settings.DB_SCHEMA_CORE}.users.telegram_id"),
        primary_key=True,
        index=True,
        nullable=False,
    )

    # NFT, который дал право на VIP (для аудита и обратной проверки)
    nft_address = Column(String, nullable=True, index=True)

    # Дата активации VIP
    activated_at = Column(DateTime, default=datetime.utcnow)

    # ORM-связь к пользователю
    user = relationship("User", back_populates="vip")

    def __repr__(self) -> str:
        return f"<UserVIP tg={self.telegram_id} nft={self.nft_address!r} at={self.activated_at}>"


class DailyGenerationLog(Base):
    """
    Лог ежедневной генерации kWh.

    Назначение:
      • Фиксируем, сколько kWh было начислено пользователю в конкретную дату.
      • Используется для предотвращения повторного начисления в один и тот же день.
      • Даёт прозрачность/аудит: сколько панелей было учтено, был ли VIP.

    Поля:
      • telegram_id     — кому начислили.
      • run_date        — дата начисления (DATE, по UTC; планировщик работает по UTC).
      • generated_kwh   — начисленная величина (учитывает VIP).
      • panels_count    — сколько активных панелей было учтено.
      • vip             — был ли у пользователя VIP на момент начисления.
      • created_at      — когда записали лог.

    Индексы:
      • уникальный (telegram_id, run_date) — для защиты от дублей начислений.
    """
    __tablename__ = "daily_generation_log"
    __table_args__ = (
        UniqueConstraint("telegram_id", "run_date", name="uq_core_dailygen_tg_date"),
        Index("ix_core_dailygen_tg_date", "telegram_id", "run_date"),
        {"schema": settings.DB_SCHEMA_CORE},
    )

    id = Column(Integer, primary_key=True)

    # Кому начислили kWh
    telegram_id = Column(
        Integer,
        ForeignKey(f"{settings.DB_SCHEMA_CORE}.users.telegram_id"),
        index=True,
        nullable=False,
    )

    # Дата (без времени) — чтобы начислять строго один раз в сутки
    run_date = Column(Date, default=date.today, nullable=False)

    # Начисленное количество kWh (Decimal)
    generated_kwh = Column(Numeric(20, settings.KWH_DECIMALS), nullable=False)

    # Сколько активных панелей учтено при расчёте
    panels_count = Column(Integer, default=0)

    # Был ли VIP-статус в момент начисления
    vip = Column(Boolean, default=False)

    # Время создания записи (аудит)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<DailyGenerationLog tg={self.telegram_id} date={self.run_date} kwh={self.generated_kwh} vip={self.vip}>"


# =============================================================================
# СХЕМА efhc_admin — Whitelist NFT для доступа к админ-панели
# =============================================================================

class AdminNFTWhitelist(Base):
    """
    Белый список NFT, которые дают доступ к админ-панели.

    Назначение:
      • Если у пользователя есть один из NFT из этого списка — он считается админом.
      • Список расширяется через админку (таблица редактируема).

    Поля:
      • nft_address — адрес NFT (уникальный).
      • added_at    — когда добавили.
      • added_by    — Telegram ID того, кто добавил (для аудита).
    """
    __tablename__ = "admin_nft_whitelist"
    __table_args__ = (
        UniqueConstraint("nft_address", name="uq_admin_whitelist_nft"),
        Index("ix_admin_whitelist_nft", "nft_address"),
        {"schema": settings.DB_SCHEMA_ADMIN},
    )

    id = Column(Integer, primary_key=True)

    # Уникальный адрес NFT (string); для TON — это строковый ID NFT.
    nft_address = Column(String, unique=True, nullable=False)

    # Время добавления в whitelist
    added_at = Column(DateTime, default=datetime.utcnow)

    # Telegram ID пользователя-админа, который добавил запись (для аудита)
    added_by = Column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<AdminNFTWhitelist id={self.id} nft={self.nft_address!r}>"


# =============================================================================
# СХЕМА efhc_referrals — Реферальная программа
# =============================================================================

class Referral(Base):
    """
    Реферальные связи: кто кого пригласил.

    Назначение:
      • Строим "дерево" приглашений — у каждого приглашённого фиксируется его пригласитель.
      • Используется для начислений за приглашения и подсчёта достижений.

    Поля:
      • inviter_id — Telegram ID того, кто пригласил.
      • invited_id — Telegram ID приглашённого пользователя.

    Индексы и ограничения:
      • Можно запретить повторную запись для одной пары (inviter_id, invited_id),
        но оставим без явного UniqueConstraint, если хотите поддерживать историчность в будущем.
    """
    __tablename__ = "referrals"
    __table_args__ = (
        Index("ix_ref_referrals_inviter", "inviter_id"),
        Index("ix_ref_referrals_invited", "invited_id"),
        {"schema": settings.DB_SCHEMA_REFERRAL},
    )

    id = Column(Integer, primary_key=True)

    inviter_id = Column(
        Integer,
        ForeignKey(f"{settings.DB_SCHEMA_CORE}.users.telegram_id"),
        nullable=False,
        index=True,
    )

    invited_id = Column(
        Integer,
        ForeignKey(f"{settings.DB_SCHEMA_CORE}.users.telegram_id"),
        nullable=False,
        index=True,
    )

    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Referral inviter={self.inviter_id} invited={self.invited_id}>"


# =============================================================================
# СХЕМА efhc_lottery — Розыгрыши и билеты
# =============================================================================

class LotteryRound(Base):
    """
    Розыгрыш (лотерея).

    Назначение:
      • Хранит метаданные розыгрыша (название, тип приза, целевое число участников).
      • Приз может быть: VIP_NFT или PANEL (см. бизнес-логику в сервисах).

    Поля:
      • title               — заголовок розыгрыша.
      • prize_type          — "VIP_NFT" | "PANEL" (строка).
      • target_participants — целевое число участников (для прогресс-бара).
      • finished            — закрыт ли розыгрыш.
    """
    __tablename__ = "lottery_rounds"
    __table_args__ = (
        Index("ix_lot_rounds_finished", "finished"),
        {"schema": settings.DB_SCHEMA_LOTTERY},
    )

    id = Column(Integer, primary_key=True)

    # Название/тема розыгрыша (для UI)
    title = Column(String, nullable=False)

    # Тип приза: "VIP_NFT" или "PANEL" (строка; можно вынести в Enum в будущем)
    prize_type = Column(String, nullable=False)

    # Целевой объём участников (для управляемости и красивого прогресс-бара)
    target_participants = Column(Integer, default=0)

    # Когда создан
    created_at = Column(DateTime, default=datetime.utcnow)

    # Флаг — завершён ли розыгрыш (розыгрыш проведён, победители определены)
    finished = Column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<LotteryRound id={self.id} title={self.title!r} finished={self.finished}>"


class LotteryTicket(Base):
    """
    Билет розыгрыша (одна запись = один купленный билет).

    Назначение:
      • Позволяет пользователю купить несколько билетов на один розыгрыш.
      • На билеты расходуются EFHC (согласно настройкам).
      • Используется при розыгрыше для выбора победителя (случайным образом по билетам).

    Поля:
      • lottery_id  — на какой розыгрыш билет.
      • telegram_id — чей билет.
      • purchased_at — когда куплен.

    Индексы:
      • (lottery_id, telegram_id) — для подсчёта билетов пользователя.
      • telegram_id — для быстрого выборки всех билетов пользователя.
    """
    __tablename__ = "lottery_tickets"
    __table_args__ = (
        Index("ix_lot_tickets_lottery_tg", "lottery_id", "telegram_id"),
        Index("ix_lot_tickets_tg", "telegram_id"),
        {"schema": settings.DB_SCHEMA_LOTTERY},
    )

    id = Column(Integer, primary_key=True)

    lottery_id = Column(
        Integer,
        ForeignKey(f"{settings.DB_SCHEMA_LOTTERY}.lottery_rounds.id"),
        nullable=False,
        index=True,
    )

    telegram_id = Column(
        Integer,
        ForeignKey(f"{settings.DB_SCHEMA_CORE}.users.telegram_id"),
        nullable=False,
        index=True,
    )

    purchased_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<LotteryTicket id={self.id} lottery={self.lottery_id} tg={self.telegram_id}>"


# =============================================================================
# СХЕМА efhc_tasks — Задания и прогресс пользователей
# =============================================================================

class Task(Base):
    """
    Задание (для выполнения пользователем).

    Назначение:
      • Содержит описание задания, вознаграждение bonus EFHC, тип цены (если используется внешняя интеграция).
      • На стороне фронта отображается список, пользователь выполняет и получает бонусы.

    Поля:
      • code                — уникальный код задания (для поиска/синхронизации).
      • title               — заголовок задания (UI).
      • reward_bonus_efhc   — награда в бонусных EFHC (Decimal).
      • price_usd           — "номинальная цена" задания (опционально).

    Индексы:
      • code — уникальный.
    """
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("code", name="uq_tasks_code"),
        Index("ix_tasks_code", "code"),
        {"schema": settings.DB_SCHEMA_TASKS},
    )

    id = Column(Integer, primary_key=True)

    # Уникальный код задания, например: "JOIN_CHANNEL", "FOLLOW_TWITTER", ...
    code = Column(String, nullable=False)

    # Заголовок/краткое описание
    title = Column(String, nullable=False)

    # Награда (bonus EFHC), будет прибавлена к Balance.bonus
    reward_bonus_efhc = Column(Numeric(20, settings.EFHC_DECIMALS), default=Decimal("1.000"))

    # Номинальная цена задания (если используется как коммерческий параметр)
    price_usd = Column(Numeric(20, 2), default=Decimal("0.30"))

    # Когда задание создано
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<Task id={self.id} code={self.code!r} reward={self.reward_bonus_efhc}>"


class TaskUserProgress(Base):
    """
    Прогресс пользователя по заданиям.

    Назначение:
      • Хранит статус выполнения задания пользователем (pending/completed/verified).
      • На основании этого начисляются бонусные EFHC.

    Поля:
      • task_id     — какое задание.
      • telegram_id — кто выполняет.
      • status      — состояние ("pending" | "completed" | "verified").
      • updated_at  — когда менялось.

    Индексы:
      • (task_id, telegram_id) — частые запросы и проверки.
    """
    __tablename__ = "task_user_progress"
    __table_args__ = (
        Index("ix_task_progress_task_tg", "task_id", "telegram_id"),
        {"schema": settings.DB_SCHEMA_TASKS},
    )

    id = Column(Integer, primary_key=True)

    task_id = Column(
        Integer,
        ForeignKey(f"{settings.DB_SCHEMA_TASKS}.tasks.id"),
        nullable=False,
        index=True,
    )

    telegram_id = Column(
        Integer,
        ForeignKey(f"{settings.DB_SCHEMA_CORE}.users.telegram_id"),
        nullable=False,
        index=True,
    )

    # Статус выполнения:
    #   pending   — задание доступно, не выполнено
    #   completed — пользователь сообщил о выполнении (или бот зафиксировал), ожидает верификацию
    #   verified  — данные подтверждены, бонус начислен
    status = Column(String, default="pending")

    updated_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<TaskUserProgress id={self.id} task={self.task_id} tg={self.telegram_id} status={self.status!r}>"
