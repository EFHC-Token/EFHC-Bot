# 📂 backend/app/utils.py — утилиты (округление, Decimal, безопасность)
# -----------------------------------------------------------------------------
from decimal import Decimal, ROUND_HALF_UP, getcontext
from typing import Tuple

getcontext().prec = 28

def dec(x) -> Decimal:
    """Безопасно приводит к Decimal с 3 знаками."""
    return Decimal(str(x))

def q3(x: Decimal) -> Decimal:
    """Округляет к 3 знакам после запятой по правилам HALF_UP."""
    return x.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

def split_spend(amount: Decimal, bonus_avail: Decimal, main_avail: Decimal) -> Tuple[Decimal, Decimal]:
    """
    Списывает сначала бонусные монеты, остаток — с основного.
    Возвращает (use_bonus, use_main).
    """
    use_bonus = min(bonus_avail, amount)
    remaining = amount - use_bonus
    use_main = remaining if remaining > 0 else Decimal("0")
    return q3(use_bonus), q3(use_main)

