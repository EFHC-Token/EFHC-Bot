# 📂 backend/app/schemas.py — Pydantic-схемы для API EFHC
# --------------------------------------------------------
# Все контракты API (FastAPI):
# - Пользователи, панели, задания, лотереи, транзакции, NFT-доступ
# - Входные payload’ы (создание, обновление, покупка, вывод)
# - Ответы для фронта, бота и админки

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, List
from decimal import Decimal
from datetime import datetime


# ======================
# ⚖️ Балансы
# ======================
class Balance(BaseModel):
    main_balance: Decimal = Field(..., description="Основной EFHC")
    bonus_balance: Decimal = Field(..., description="Бонусные EFHC")
    kwh_balance: Decimal = Field(..., description="Энергия кВт")
    total_generated_kwh: Decimal = Field(..., description="Накопленная генерация за всё время")


# ======================
# 👤 Пользователи
# ======================
class UserBase(BaseModel):
    telegram_id: str
    username: Optional[str] = None
    ton_wallet: Optional[str] = None


class UserCreate(UserBase):
    pass


class UserUpdate(BaseModel):
    username: Optional[str] = None
    ton_wallet: Optional[str] = None


class UserResponse(UserBase):
    id: int
    created_at: datetime
    is_vip: bool = False
    vip_checked_at: Optional[datetime] = None
    referral_code: Optional[str]
    invited_by: Optional[int]
    current_level: int = 1
    is_admin: bool = False

    balance_main: Decimal
    balance_bonus: Decimal
    balance_kwt: Decimal
    total_kwt_generated: Decimal

    class Config:
        orm_mode = True


class UserPublic(BaseModel):
    id: int
    telegram_id: int
    username: Optional[str]
    is_vip: bool = False
    language: Optional[str] = "RU"
    balance: Balance


# ======================
# 🔋 Панели
# ======================
class PanelBase(BaseModel):
    daily_generation: Decimal = Decimal("0.598")
    expires_at: datetime


class PanelCreate(PanelBase):
    pass


class PanelResponse(PanelBase):
    id: int
    user_id: int
    created_at: datetime
    is_active: bool

    class Config:
        orm_mode = True


class PanelOut(BaseModel):
    id: int
    purchase_date: str
    lifespan_days: int
    daily_generation: Decimal
    active: bool


# ======================
# 👥 Рефералы
# ======================
class ReferralStats(BaseModel):
    direct_count: int = 0
    active_direct_count: int = 0
    bonuses_total_efhc: Decimal = Decimal("0.000")


class ReferralBonusBase(BaseModel):
    user_id: int
    invited_user_id: int
    amount: Decimal


class ReferralBonusResponse(ReferralBonusBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True


# ======================
# 💸 Транзакции EFHC
# ======================
class TransactionBase(BaseModel):
    amount: Decimal
    type: str
    comment: Optional[str] = None


class TransactionCreate(TransactionBase):
    user_id: int


class TransactionResponse(TransactionBase):
    id: int
    user_id: int
    created_at: datetime

    class Config:
        orm_mode = True


# ======================
# 📌 Задания
# ======================
class TaskBase(BaseModel):
    title: str
    reward: Decimal
    link: Optional[str] = None
    is_active: bool = True


class TaskCreate(TaskBase):
    available: int = Field(..., ge=1)


class TaskResponse(TaskBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True


class TaskOut(BaseModel):
    id: int
    type: str
    title: str
    url: Optional[str]
    available_left: int
    reward_bonus_efhc: Decimal
    is_active: bool


class TaskCompleteRequest(BaseModel):
    task_id: int
    proof: Optional[str] = Field(None, description="Опциональное доказательство (скрин/хеш/комментарий)")


class UserTaskBase(BaseModel):
    user_id: int
    task_id: int
    status: str = "pending"


class UserTaskResponse(UserTaskBase):
    id: int
    completed_at: Optional[datetime] = None

    class Config:
        orm_mode = True


# ======================
# 🎲 Лотереи
# ======================
class LotteryBase(BaseModel):
    title: str
    required_tickets: int
    reward_type: str
    is_active: bool = True


class LotteryCreate(LotteryBase):
    pass


class LotteryResponse(LotteryBase):
    id: int
    created_at: datetime
    winner_id: Optional[int] = None

    class Config:
        orm_mode = True


class LotteryOut(BaseModel):
    id: int
    code: str
    title: str
    is_active: bool
    target_participants: int
    current_participants: int
    prize_type: str
    winner_user_id: Optional[int]


class LotteryTicketBase(BaseModel):
    user_id: int
    lottery_id: int


class LotteryTicketResponse(LotteryTicketBase):
    id: int
    ticket_number: int

    class Config:
        orm_mode = True


class TicketPurchaseRequest(BaseModel):
    lottery_id: int
    tickets: int = Field(..., ge=1, le=10)


# ======================
# 🏆 Достижения
# ======================
class AchievementLogBase(BaseModel):
    user_id: int
    level: int


class AchievementLogResponse(AchievementLogBase):
    id: int
    achieved_at: datetime

    class Config:
        orm_mode = True


# ======================
# 🛒 Магазин
# ======================
class ShopItemOut(BaseModel):
    id: int
    code: str
    label: str
    pay_asset: str
    price: Decimal
    is_active: bool


# ======================
# ↔️ Обмен и вывод
# ======================
class ExchangeRequest(BaseModel):
    kwh_amount: Decimal = Field(..., gt=Decimal("0.000"))


class PanelBuyResponse(BaseModel):
    success: bool
    panel_id: Optional[int]
    charged_bonus: Decimal = Decimal("0.000")
    charged_main: Decimal = Decimal("0.000")
    balances_after: Balance


class WithdrawRequestIn(BaseModel):
    amount_efhc: Decimal = Field(..., gt=Decimal("0.000"))
    to_wallet: str


# ======================
# 🔑 Админка
# ======================
class ToggleIn(BaseModel):
    id: int
    active: bool


class AdminVipConfirmIn(BaseModel):
    request_id: int
    tx: Optional[str] = None


class AdminNFTBase(BaseModel):
    nft_address: str
    can_tasks: bool = False
    can_shop: bool = False
    can_lottery: bool = False
    can_all: bool = False


class AdminNFTCreate(AdminNFTBase):
    pass


class AdminNFTResponse(AdminNFTBase):
    id: int

    class Config:
        orm_mode = True
