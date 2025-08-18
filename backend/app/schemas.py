# 📂 backend/app/schemas.py — Pydantic-схемы для API
# Эти схемы описывают формат данных, которые принимает и возвращает наш сервер (FastAPI).
# Они нужны для валидации входных запросов и формирования корректных ответов пользователю/админу.

from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


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
    balance_main: float
    balance_bonus: float
    balance_kwt: float
    is_vip: bool
    vip_checked_at: datetime
    referral_code: Optional[str]
    invited_by: Optional[int]
    current_level: int
    total_kwt_generated: float
    is_admin: bool

    class Config:
        orm_mode = True


# ======================
# 🔋 Панели
# ======================
class PanelBase(BaseModel):
    daily_generation: float = 0.598
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


# ======================
# 💸 Транзакции EFHC
# ======================
class TransactionBase(BaseModel):
    amount: float
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
# 👥 Рефералы
# ======================
class ReferralBonusBase(BaseModel):
    user_id: int
    invited_user_id: int
    amount: float


class ReferralBonusResponse(ReferralBonusBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True


# ======================
# 📌 Задания
# ======================
class TaskBase(BaseModel):
    title: str
    reward: float
    link: Optional[str] = None
    is_active: bool = True


class TaskCreate(TaskBase):
    pass


class TaskResponse(TaskBase):
    id: int
    created_at: datetime

    class Config:
        orm_mode = True


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
# 🎲 Розыгрыши
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


class LotteryTicketBase(BaseModel):
    user_id: int
    lottery_id: int


class LotteryTicketResponse(LotteryTicketBase):
    id: int
    ticket_number: int

    class Config:
        orm_mode = True


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
# 🔑 Админ NFT-доступ
# ======================
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
