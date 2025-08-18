# 📂 backend/app/admin_routes.py — API для администраторов (управление EFHC, розыгрыши, задания, магазины)

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from .config import get_settings

# Здесь импортируем функции для cron-задач
from .nft_checker import run_daily_nft_check
from .scheduler import run_daily_energy_accrual

router = APIRouter()
settings = get_settings()

# -----------------------------------------------------------------------------
# Простейшая проверка секретного ключа (для cron)
# -----------------------------------------------------------------------------
def verify_secret(secret: str = Query(..., description="Secret key for cron tasks")):
    if secret != os.getenv("ADMIN_CRON_SECRET", "changeme"):
        raise HTTPException(status_code=403, detail="Invalid secret key")
    return True

# -----------------------------------------------------------------------------
# Cron API ручка: Проверка NFT
# -----------------------------------------------------------------------------
@router.post("/run_nft_check")
async def run_nft_check(secret: bool = Depends(verify_secret)):
    """
    Запуск проверки VIP NFT вручную (используется cron-job.org или GitHub Actions).
    """
    result = await run_daily_nft_check()
    return JSONResponse({"status": "ok", "task": "nft_check", "result": result})

# -----------------------------------------------------------------------------
# Cron API ручка: Начисление энергии
# -----------------------------------------------------------------------------
@router.post("/run_energy_accrual")
async def run_energy_accrual(secret: bool = Depends(verify_secret)):
    """
    Запуск начисления кВт вручную (используется cron-job.org или GitHub Actions).
    """
    result = await run_daily_energy_accrual()
    return JSONResponse({"status": "ok", "task": "energy_accrual", "result": result})

