# 📂 backend/app/utils.py — общие утилиты (Decimal, округления, хелперы, локализация, списания)
# -----------------------------------------------------------------------------
# Здесь:
# - безопасная работа с Decimal (денежные/энергетические значения с 3 знаками),
# - split_spend (списание бонусных/основных EFHC),
# - округление до 3х знаков (q3),
# - генерация ID (UUID),
# - валидация сумм, проверка лимитов,
# - простая локализация (8 языков) для статичных фрагментов (бот и WebApp),
# - хелперы для прогресса уровня, расчёта генерации, проверки лимитов лотереи.

from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import Dict, Any, Optional, Tuple, List
import uuid
from .config import get_settings

settings = get_settings()
getcontext().prec = 28  # высокая точность внутренних вычислений

# =========================
# 🔢 Работа с Decimal
# =========================
def dec(x: Any) -> Decimal:
    """Безопасно приводит к Decimal"""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))

def q3(x: Any) -> Decimal:
    """Округляет к 3 знакам после запятой (банковское округление HALF_UP)."""
    return dec(x).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

def split_spend(amount: Decimal, bonus_avail: Decimal, main_avail: Decimal) -> Tuple[Decimal, Decimal]:
    """
    Списывает сначала бонусные монеты, остаток — с основного.
    Возвращает (use_bonus, use_main).
    """
    use_bonus = min(bonus_avail, amount)
    remaining = amount - use_bonus
    use_main = remaining if remaining > 0 else Decimal("0")
    return q3(use_bonus), q3(use_main)

# =========================
# 🆔 UUID
# =========================
def gen_uuid() -> str:
    """Генерация человекочитаемого UUID (для внутренних меток)."""
    return str(uuid.uuid4())

# =========================
# 📈 Прогресс уровня
# =========================
def level_progress(levels: List[Dict[str, str]], current_kwh: Decimal) -> Tuple[int, Optional[Decimal], Decimal]:
    """
    levels = список словарей: idx, name, threshold_kwh (строка)
    current_kwh = Decimal наработанных кВт
    Возвращает: (current_level_idx:int, next_threshold:Decimal|None, progress_0_1:Decimal)
    """
    current_kwh = dec(current_kwh)
    active_idx = 1
    next_thr: Optional[Decimal] = None
    for lvl in levels:
        thr = dec(lvl["threshold_kwh"])
        if current_kwh >= thr:
            active_idx = int(lvl["idx"])
        else:
            next_thr = thr
            break
    if next_thr is None:
        return active_idx, None, dec("1.0")

    # Находим предыдущий threshold
    prev_thr = dec("0")
    for lvl in levels:
        if int(lvl["idx"]) == active_idx:
            prev_thr = dec(lvl["threshold_kwh"])
            break
    span = next_thr - prev_thr
    done = current_kwh - prev_thr
    progress = dec("0.0")
    if span > 0:
        progress = (done / span)
        if progress < 0:
            progress = dec("0.0")
        if progress > 1:
            progress = dec("1.0")
    return active_idx, next_thr, progress.quantize(Decimal("0.001"))

# =========================
# ⚡ Генерация энергии
# =========================
def daily_generation_kwh(is_vip: bool) -> Decimal:
    """Расчёт дневной генерации для пользователя: VIP → DAILY_GEN_VIP_KWH, иначе DAILY_GEN_BASE_KWH"""
    return q3(settings.DAILY_GEN_VIP_KWH if is_vip else settings.DAILY_GEN_BASE_KWH)

# =========================
# 🎟 Лотерея
# =========================
def can_buy_more_tickets(current_count: int, add: int) -> bool:
    """Проверка лимита лотерейных билетов пользователя."""
    return (current_count + add) <= settings.LOTTERY_MAX_TICKETS_PER_USER

# =========================
# 🌍 Локализация
# =========================
I18N: Dict[str, Dict[str, str]] = {
    "RU": {
        "PANELS_PURCHASE_OK": "✅ Панель успешно куплена. Списано: {bonus} бонусных EFHC и {main} EFHC.",
        "PANELS_FUNDS_ERROR": "❌ Недостаточно средств: {total} EFHC (бонусных {bonus} + основных {main}). Нужно 100.000.",
        "TASK_DONE_BONUS": "✅ Задание выполнено! +{bonus} бонусных EFHC.",
        "LOTTERY_TICKET_BOUGHT": "🎟 Билет(ы) куплены: {n}. Всего твоих билетов: {total}.",
    },
    "EN": {
        "PANELS_PURCHASE_OK": "✅ Panel purchased. Charged: {bonus} bonus EFHC and {main} EFHC.",
        "PANELS_FUNDS_ERROR": "❌ Insufficient funds: {total} EFHC (bonus {bonus} + main {main}). Need 100.000.",
        "TASK_DONE_BONUS": "✅ Task completed! +{bonus} bonus EFHC.",
        "LOTTERY_TICKET_BOUGHT": "🎟 Ticket(s) bought: {n}. Your total tickets: {total}.",
    }
    # Остальные языки держим на фронте; тут — только критичные фразы.
}

def t(key: str, lang: Optional[str] = None, **kwargs) -> str:
    """Простая локализация строк."""
    lang = (lang or settings.DEFAULT_LANG).upper()
    mp = I18N.get(lang) or I18N.get("RU")
    s = mp.get(key) or I18N["RU"].get(key) or key
    return s.format(**kwargs)
