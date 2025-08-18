# 📂 backend/app/scheduler.py — задачи, которые раньше запускались по расписанию
# Теперь мы их вызываем вручную через API (admin_routes.py)

async def run_daily_nft_check():
    """
    Проверка NFT VIP у пользователей.
    TODO: здесь логика проверки через GetGems API (пока заглушка).
    """
    print("[Scheduler] Проверка NFT VIP запущена")
    return {"checked": 123, "vip_detected": 5}

async def run_daily_energy_accrual():
    """
    Начисление ежедневной энергии пользователям.
    TODO: здесь логика начисления кВт пользователям (с VIP множителем).
    """
    print("[Scheduler] Начисление энергии запущено")
    return {"processed_users": 456}

