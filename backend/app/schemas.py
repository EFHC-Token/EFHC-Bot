# backend/app/schemas.py
from __future__ import annotations
from typing import Optional, List, Dict
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, Field, conint, constr

# Pydantic v2 config: allow constructing from ORM objects
base_model_config = {"from_attributes": True}


# ---------------------------
# Общие типы
# ---------------------------
class TimestampedModel(BaseModel):
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = base_model_config


# ---------------------------
# Пользователь
# ---------------------------
class UserCreate(BaseModel):
    telegram_id: int = Field(..., description="Telegram ID пользователя")
    username: Optional[str] = Field(None, description="Никнейм в Telegram")
    language: Optional[str] = Field(None, description="Язык интерфейса (код)")

    model_config = base_model_config


class UserOut(BaseModel):
    id: str
    telegram_id: int
    username: Optional[str]
    language: str

    balance_efhc: Decimal = Field(..., description="Баланс EFHC (точность до 3 знаков)")
    balance_kwh_total_generated: Decimal = Field(..., description="Всего сгенерировано кВт")
    balance_kwh_available_for_exchange: Decimal = Field(..., description="Доступно к обмену кВт")

    is_active_user: bool
    is_vip: bool
    last_nft_check_at: Optional[datetime]

    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    model_config = base_model_config


# ---------------------------
# Кошельки пользователя
# ---------------------------
class UserWalletIn(BaseModel):
    chain: str
    address: str
    memo_template: Optional[str] = None

    model_config = base_model_config


class UserWalletOut(BaseModel):
    id: str
    chain: str
    address: str
    memo_template: Optional[str]
    created_at: Optional[datetime]

    model_config = base_model_config


# ---------------------------
# Панели
# ---------------------------
class PanelCreate(BaseModel):
    # Покупка панели происходит через API без явного тела, но схему держим для полноты
    pass

class PanelOut(BaseModel):
    id: str
    start_at: datetime
    end_at: datetime
    status: str
    label: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    days_left: Optional[int] = Field(None, description="Оставшиеся дни (вручную заполняется сервисом)")

    model_config = base_model_config


# ---------------------------
# Начисления энергии (accruals)
# ---------------------------
class EnergyAccrualOut(BaseModel):
    id: str
    user_id: str
    accrual_date: datetime
    amount_kwh: Decimal
    created_at: Optional[datetime]

    model_config = base_model_config


# ---------------------------
# Транзакции
# ---------------------------
class TransactionCreate(BaseModel):
    user_id: str
    tx_type: str
    amount_efhc: Decimal
    memo: Optional[str] = None
    external_tx_id: Optional[str] = None
    external_chain: Optional[str] = None

    model_config = base_model_config


class TransactionOut(BaseModel):
    id: str
    user_id: str
    tx_type: str
    amount_efhc: Decimal
    memo: Optional[str]
    external_tx_id: Optional[str]
    external_chain: Optional[str]
    created_at: Optional[datetime]

    model_config = base_model_config


# ---------------------------
# Рефералы
# ---------------------------
class ReferralLinkOut(BaseModel):
    id: str
    inviter_user_id: str
    code: str
    created_at: Optional[datetime]

    model_config = base_model_config


class ReferralRelationOut(BaseModel):
    id: str
    inviter_user_id: str
    invitee_user_id: str
    active: bool
    bonuses_paid_efhc: Decimal
    created_at: Optional[datetime]

    model_config = base_model_config


class ReferralMilestoneOut(BaseModel):
    id: str
    user_id: str
    threshold: int
    bonus_efhc: Decimal
    granted_at: datetime

    model_config = base_model_config


# ---------------------------
# Магазин (Shop)
# ---------------------------
class ShopPackageIn(BaseModel):
    package_id: str
    title: str
    pay_asset: str
    price: Decimal
    payload_json: Optional[Dict] = None
    enabled: Optional[bool] = True

    model_config = base_model_config


class ShopPackageOut(BaseModel):
    id: str
    package_id: str
    title: str
    pay_asset: str
    price: Decimal
    payload_json: Optional[Dict]
    enabled: bool
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    model_config = base_model_config


# ---------------------------
# Feature flag / Admin
# ---------------------------
class FeatureFlagOut(BaseModel):
    id: str
    key: str
    value_bool: Optional[bool]
    value_text: Optional[str]
    value_json: Optional[Dict]
    updated_by_admin_id: Optional[str]
    updated_at: Optional[datetime]

    model_config = base_model_config


class AdminOut(BaseModel):
    id: str
    telegram_id: int
    role: str
    permissions_json: Optional[Dict]
    nft_required: bool
    enabled: bool
    created_at: Optional[datetime]

    model_config = base_model_config


# ---------------------------
# Лотереи (Raffles / Lotteries)
# ---------------------------
class LotteryCreate(BaseModel):
    code: str = Field(..., description="Уникальный код лотереи")
    title: str
    description: Optional[str] = None
    prize_type: str  # VIP_NFT | PANEL | EFHC
    target_participants: int
    ticket_price_efhc: Decimal = Field(..., description="Цена билета в EFHC")
    max_tickets_per_user: int = Field(..., description="Макс билетов/пользователь")
    enabled: bool = True
    auto_draw: bool = True

    model_config = base_model_config


class LotteryOut(BaseModel):
    id: str
    code: str
    title: str
    description: Optional[str]
    prize_type: str
    target_participants: int
    ticket_price_efhc: Decimal
    max_tickets_per_user: int
    enabled: bool
    auto_draw: bool
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    finished_at: Optional[datetime]

    # Доп. поля для фронтенда (наполняет сервис/роутер)
    current_tickets: Optional[int] = 0
    participants_count: Optional[int] = 0

    model_config = base_model_config


class LotteryTicketBuyIn(BaseModel):
    tickets: conint(ge=1, le=100) = Field(1, description="Количество билетов (в рамках лимита лотереи)")

    model_config = base_model_config


class LotteryTicketOut(BaseModel):
    id: str
    lottery_id: str
    user_id: str
    serial: int
    created_at: Optional[datetime]

    model_config = base_model_config


class LotteryWinnerOut(BaseModel):
    id: str
    lottery_id: str
    user_id: str
    ticket_id: Optional[str]
    prize_type: str
    granted: bool
    created_at: Optional[datetime]
    granted_at: Optional[datetime]

    model_config = base_model_config


# ---------------------------
# Exchange (kWh -> EFHC)
# ---------------------------
class ExchangeIn(BaseModel):
    kwh: Decimal = Field(..., description="Сколько kWh конвертировать (минимум задан в конфигах)")

    model_config = base_model_config


class ExchangeOut(BaseModel):
    efhc_received: Decimal
    kwh_spent: Decimal

    model_config = base_model_config


# ---------------------------
# Рейтинги и списки (Top)
# ---------------------------
class RatingUserItem(BaseModel):
    position: int
    user_id: str
    username: Optional[str]
    value_kwh: Decimal

    model_config = base_model_config


class ReferralRatingItem(BaseModel):
    position: int
    user_id: str
    username: Optional[str]
    active_referrals: int
    bonus_received_efhc: Decimal

    model_config = base_model_config


class RatingResponse(BaseModel):
    user: RatingUserItem
    top: List[RatingUserItem]

    model_config = base_model_config


class ReferralRatingResponse(BaseModel):
    user: ReferralRatingItem
    top: List[ReferralRatingItem]

    model_config = base_model_config


# ---------------------------
# Утилиты API
# ---------------------------
class OKResponse(BaseModel):
    ok: bool = True
    detail: Optional[str] = None

    model_config = base_model_config


# ---------------------------
# Настройки фронтенду (мини-API)
# ---------------------------
class FrontendSettingsOut(BaseModel):
    project_name: str
    supported_langs: List[str]
    default_lang: str
    panel_price_efhc: Decimal
    panel_lifespan_days: int
    levels: List[Dict]

    model_config = base_model_config

