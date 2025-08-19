# 📂 backend/app/services/core.py — бизнес-логика EFHC Bot
# =============================================================================
# Назначение:
#   Этот модуль содержит основные операции над сущностями:
#     • Пользователи (регистрация и профили)
#     • Балансы (EFHC / бонусы / kWh)
#     • Покупка панелей (100 EFHC, раздельное списание бонус→основной)
#     • Обменник (kWh → EFHC 1:1)
#     • Розыгрыши (список, покупка билетов)
#     • Задания (список, выполнение, начисление бонусов)
#     • Проверка админ-прав (базово по ADMIN_TELEGRAM_ID + точки расширения под NFT)
#
# Как использовать:
#   Вызывается из FastAPI-роутеров (user_routes.py/admin_routes.py) и/или из Telegram-бота.
#
# Примечания:
#   • Все денежные величины — Decimal (NUMERIC). Для форматирования/округления — используйте quantize().
#   • Строго соблюдаем множитель VIP = 1.07 (см. config.py), логика начислений в scheduler.py.
#   • Лотереи/Задания имеют "ленивую инициализацию" — если таблицы пустые, создаём дефолты из settings.
# =============================================================================

from __future__ import annotations

from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_DOWN
from typing import Optional, List, Dict, Tuple

from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import (
    User,
    Balance,
    Panel,
    UserVIP,
    DailyGenerationLog,
    AdminNFTWhitelist,
    Referral,
    LotteryRound,
    LotteryTicket,
    Task,
    TaskUserProgress,
)

settings = get_settings()

# Удобные константы
PANEL_PRICE = Decimal(f"{settings.PANEL_PRICE_EFHC:.3f}")          # 100.000 EFHC
EFHC_Q = Decimal("0." + "0"*(settings.EFHC_DECIMALS-1) + "1")      # шаг округления EFHC (напр. 0.001)
KWH_Q  = Decimal("0." + "0"*(settings.KWH_DECIMALS-1) + "1")       # шаг округления kWh  (напр. 0.001)


# =============================================================================
# Вспомогательные функции форматирования/округления
# =============================================================================

def fmt_e(d: Decimal) -> str:
    """
    Форматирует Decimal в строку с нужной точностью EFHC_DECIMALS (например, 3 знака).
    """
    return str(d.quantize(EFHC_Q, rounding=ROUND_DOWN))


def fmt_k(d: Decimal) -> str:
    """
    Форматирует Decimal в строку с точностью KWH_DECIMALS (например, 3 знака).
    """
    return str(d.quantize(KWH_Q, rounding=ROUND_DOWN))


# =============================================================================
# Пользователи / регистрация / баланс
# =============================================================================

async def get_or_create_user(db: AsyncSession, telegram_id: int, username: Optional[str] = None) -> User:
    """
    Идемпотентная регистрация пользователя:
      • создаёт User и Balance, если их ещё нет,
      • обновляет username, если изменился.

    Возвращает объект User.
    """
    # Пытаемся найти пользователя по telegram_id
    res = await db.execute(select(User).where(User.telegram_id == telegram_id))
    user = res.scalar_one_or_none()

    if user is None:
        # Создаём User и связанный Balance
        user = User(telegram_id=telegram_id, username=username or None, lang=settings.DEFAULT_LANG)
        balance = Balance(telegram_id=telegram_id)
        db.add(user)
        db.add(balance)
        await db.flush()  # получаем id инстансов
    else:
        # Обновим username при необходимости
        new_username = (username or "").strip() or None
        if new_username != user.username:
            user.username = new_username
            await db.flush()

    return user


async def get_balance(db: AsyncSession, telegram_id: int) -> Dict[str, str]:
    """
    Возвращает баланс пользователя в виде словаря строк (для UI):
      {
        "efhc":  "100.000",
        "bonus": "10.000",
        "kwh":   "5.000"
      }
    """
    res = await db.execute(select(Balance).where(Balance.telegram_id == telegram_id))
    bal = res.scalar_one_or_none()
    if bal is None:
        # На всякий случай инициализация (если баланс не был создан ранее)
        bal = Balance(telegram_id=telegram_id)
        db.add(bal)
        await db.flush()

    return {
        "efhc":  fmt_e(Decimal(bal.efhc or 0)),
        "bonus": fmt_e(Decimal(bal.bonus or 0)),
        "kwh":   fmt_k(Decimal(bal.kwh or 0)),
    }


# =============================================================================
# Панели / Покупка / Активные панели
# =============================================================================

async def get_active_panels_count(db: AsyncSession, telegram_id: int) -> int:
    """
    Возвращает количество активных панелей пользователя:
      • "Активная" = expires_at is NULL (бессрочно) ИЛИ expires_at > now().
    """
    now = datetime.utcnow()
    res = await db.execute(
        select(func.coalesce(func.sum(Panel.count), 0))
        .where(
            and_(
                Panel.telegram_id == telegram_id,
                # expires_at может быть NULL (на всякий случай считаем такие панели активными)
                or_(Panel.expires_at.is_(None), Panel.expires_at > now)
            )
        )
    )
    total = res.scalar_one()
    return int(total or 0)


async def purchase_panel(db: AsyncSession, telegram_id: int) -> Dict[str, str]:
    """
    Покупка ОДНОЙ панели за 100 EFHC с комбинированным списанием:
      • Сначала расходуются бонусные EFHC,
      • Остаток — из основного баланса.
    Проверки:
      • Достаточно ли средств (bonus + efhc >= 100)
      • Не превышен ли лимит активных панелей (MAX_ACTIVE_PANELS_PER_USER)

    Возвращает:
      {
        "bonus_used": "X.XXX",
        "main_used":  "Y.YYY"
      }
    """
    # Получим баланс
    res = await db.execute(select(Balance).where(Balance.telegram_id == telegram_id))
    bal = res.scalar_one_or_none()
    if bal is None:
        raise RuntimeError("Баланс не найден. Повторите /start.")

    # Проверим лимит панелей
    now = datetime.utcnow()
    res_panels = await db.execute(
        select(func.coalesce(func.sum(Panel.count), 0)).where(
            and_(
                Panel.telegram_id == telegram_id,
                or_(Panel.expires_at.is_(None), Panel.expires_at > now)
            )
        )
    )
    active_count = int(res_panels.scalar_one() or 0)
    if active_count + 1 > settings.MAX_ACTIVE_PANELS_PER_USER:
        raise RuntimeError(f"Превышен лимит активных панелей: {settings.MAX_ACTIVE_PANELS_PER_USER}")

    # Сколько списать бонусов
    bonus_avail = Decimal(bal.bonus or 0)
    efhc_avail  = Decimal(bal.efhc or 0)

    total_avail = bonus_avail + efhc_avail
    if total_avail < PANEL_PRICE:
        raise RuntimeError(f"Недостаточно EFHC: требуется {fmt_e(PANEL_PRICE)}, доступно {fmt_e(total_avail)}")

    bonus_used = min(PANEL_PRICE, bonus_avail)
    main_needed = (PANEL_PRICE - bonus_used)
    main_used  = min(main_needed, efhc_avail)

    # Резервно проверим (не должно случиться)
    if bonus_used + main_used < PANEL_PRICE:
        raise RuntimeError("Недостаточно EFHC для покупки панели.")

    # Списываем
    bal.bonus = (bonus_avail - bonus_used).quantize(EFHC_Q, rounding=ROUND_DOWN)
    bal.efhc  = (efhc_avail - main_used).quantize(EFHC_Q, rounding=ROUND_DOWN)

    # Создаём запись о панели
    expires = now + timedelta(days=settings.PANEL_LIFESPAN_DAYS)
    p = Panel(telegram_id=telegram_id, count=1, purchased_at=now, expires_at=expires)
    db.add(p)

    await db.flush()

    return {
        "bonus_used": fmt_e(bonus_used),
        "main_used":  fmt_e(main_used),
    }


# =============================================================================
# Обменник: kWh → EFHC (1:1)
# =============================================================================

async def exchange_kwh_to_efhc(db: AsyncSession, telegram_id: int, amount_kwh: Decimal) -> Dict[str, str]:
    """
    Обмен kWh на EFHC по фиксированному курсу 1:1, учитывая минимум EXCHANGE_MIN_KWH.
    Мутирует баланс:
      • kWh уменьшаются,
      • efhc увеличивается на ту же величину.

    Возвращает:
      {
        "exchanged": "X.XXX",
        "efhc":      "new_efhc_balance",
        "kwh":       "new_kwh_balance"
      }
    """
    # Нормализуем amount_kwh
    if amount_kwh is None:
        raise RuntimeError("Не указано количество kWh для обмена.")
    try:
        amt = Decimal(str(amount_kwh)).quantize(KWH_Q, rounding=ROUND_DOWN)
    except Exception:
        raise RuntimeError("Неверный формат суммы kWh.")

    if amt <= Decimal("0"):
        raise RuntimeError("Сумма обмена должна быть > 0.")
    if amt < Decimal(str(settings.EXCHANGE_MIN_KWH)):
        raise RuntimeError(f"Минимум для обмена — {fmt_k(Decimal(str(settings.EXCHANGE_MIN_KWH)))} kWh.")

    # Баланс
    res = await db.execute(select(Balance).where(Balance.telegram_id == telegram_id))
    bal = res.scalar_one_or_none()
    if bal is None:
        raise RuntimeError("Баланс не найден.")

    kwh_avail = Decimal(bal.kwh or 0).quantize(KWH_Q, rounding=ROUND_DOWN)
    if amt > kwh_avail:
        raise RuntimeError(f"Недостаточно kWh: доступно {fmt_k(kwh_avail)}.")

    # Равный обмен 1:1 → EFHC
    efhc_new = (Decimal(bal.efhc or 0) + amt).quantize(EFHC_Q, rounding=ROUND_DOWN)
    kwh_new  = (kwh_avail - amt).quantize(KWH_Q, rounding=ROUND_DOWN)

    bal.efhc = efhc_new
    bal.kwh  = kwh_new

    await db.flush()

    return {
        "exchanged": fmt_k(amt),
        "efhc":      fmt_e(efhc_new),
        "kwh":       fmt_k(kwh_new),
    }


# =============================================================================
# Розыгрыши (ЛОТЕРЕИ) — список и покупка билетов
# =============================================================================

async def _ensure_default_lotteries(db: AsyncSession) -> None:
    """
    Ленивая инициализация: если нет ни одного розыгрыша, создаём дефолты из settings.LOTTERY_DEFAULTS.
    """
    res = await db.execute(select(func.count(LotteryRound.id)))
    cnt = int(res.scalar_one() or 0)
    if cnt > 0:
        return

    for l in settings.LOTTERY_DEFAULTS:
        obj = LotteryRound(
            title=l.get("title") or "Розыгрыш",
            prize_type=l.get("prize_type") or "PANEL",
            target_participants=int(l.get("target_participants") or 0),
            finished=False,
        )
        db.add(obj)
    await db.flush()


async def list_lotteries(db: AsyncSession) -> List[Dict]:
    """
    Возвращает список активных розыгрышей (finished=False) в виде DTO для UI.
    Пример элемента:
      {
         "id": 1,
         "title": "NFT VIP",
         "target": 500,
         "tickets_sold": 123
      }
    """
    await _ensure_default_lotteries(db)

    # Выберем все незавершённые розыгрыши
    res = await db.execute(select(LotteryRound).where(LotteryRound.finished.is_(False)).order_by(LotteryRound.id.asc()))
    rounds = res.scalars().all()

    items: List[Dict] = []
    for r in rounds:
        # Посчитаем число проданных билетов
        res_count = await db.execute(select(func.count(LotteryTicket.id)).where(LotteryTicket.lottery_id == r.id))
        sold = int(res_count.scalar_one() or 0)
        items.append({
            "id": r.id,
            "title": r.title,
            "target": r.target_participants,
            "tickets_sold": sold,
            "prize_type": r.prize_type,
        })
    return items


async def buy_lottery_tickets(db: AsyncSession, telegram_id: int, lottery_id: int, count: int) -> Dict[str, str]:
    """
    Покупка N билетов для розыгрыша:
      • Цена 1 билета = settings.LOTTERY_TICKET_PRICE_EFHC (в EFHC).
      • Ограничение: max settings.LOTTERY_MAX_TICKETS_PER_USER за одну операцию.
      • Списание EFHC с основного баланса.
    """
    if not settings.LOTTERY_ENABLED:
        raise RuntimeError("Розыгрыши отключены.")

    if count <= 0:
        raise RuntimeError("Количество билетов должно быть > 0.")
    if count > settings.LOTTERY_MAX_TICKETS_PER_USER:
        raise RuntimeError(f"За один раз можно купить максимум {settings.LOTTERY_MAX_TICKETS_PER_USER} билетов.")

    # Проверим розыгрыш
    res = await db.execute(select(LotteryRound).where(LotteryRound.id == lottery_id, LotteryRound.finished.is_(False)))
    round_ = res.scalar_one_or_none()
    if round_ is None:
        raise RuntimeError("Розыгрыш не найден или уже завершён.")

    # Баланс
    resb = await db.execute(select(Balance).where(Balance.telegram_id == telegram_id))
    bal = resb.scalar_one_or_none()
    if bal is None:
        raise RuntimeError("Баланс не найден.")

    price_per_ticket = Decimal(str(settings.LOTTERY_TICKET_PRICE_EFHC)).quantize(EFHC_Q, rounding=ROUND_DOWN)
    total_price      = (price_per_ticket * Decimal(count)).quantize(EFHC_Q, rounding=ROUND_DOWN)

    efhc_avail = Decimal(bal.efhc or 0)
    if efhc_avail < total_price:
        raise RuntimeError(f"Недостаточно EFHC: требуется {fmt_e(total_price)}, доступно {fmt_e(efhc_avail)}.")

    # Списание и создание билетов
    bal.efhc = (efhc_avail - total_price).quantize(EFHC_Q, rounding=ROUND_DOWN)
    for _ in range(count):
        db.add(LotteryTicket(lottery_id=lottery_id, telegram_id=telegram_id, purchased_at=datetime.utcnow()))

    await db.flush()

    return {
        "tickets_bought": str(count),
        "paid_efhc": fmt_e(total_price),
        "efhc_left": fmt_e(Decimal(bal.efhc or 0)),
    }


# =============================================================================
# Задания — список и выполнение
# =============================================================================

async def _ensure_default_tasks(db: AsyncSession) -> None:
    """
    Ленивая инициализация задач: если пусто — добавляем несколько базовых.
    Берём константы вознаграждения из settings (TASK_REWARD_BONUS_EFHC_DEFAULT).
    """
    res = await db.execute(select(func.count(Task.id)))
    cnt = int(res.scalar_one() or 0)
    if cnt > 0:
        return

    # Примеры базовых заданий; UI может их переименовывать/редактировать из админки
    defaults = [
        {"code": "JOIN_CHANNEL", "title": "Вступить в Telegram-канал"},
        {"code": "FOLLOW_TWITTER", "title": "Подписаться на X (Twitter)"},
        {"code": "SHARE_LINK", "title": "Поделиться реферальной ссылкой"},
    ]
    reward = Decimal(str(settings.TASK_REWARD_BONUS_EFHC_DEFAULT))
    for d in defaults:
        db.add(Task(
            code=d["code"],
            title=d["title"],
            reward_bonus_efhc=reward,
            price_usd=Decimal(str(settings.TASK_PRICE_USD_DEFAULT))
        ))
    await db.flush()


async def list_tasks(db: AsyncSession, telegram_id: int) -> List[Dict]:
    """
    Возвращает список заданий и статус пользователя по каждому.
    DTO:
      {
        "id": 1,
        "code": "JOIN_CHANNEL",
        "title": "Вступить...",
        "reward": "1.000",
        "completed": true/false,
        "status": "pending/completed/verified" (если есть)
      }
    """
    await _ensure_default_tasks(db)

    # Выбираем все задания
    res = await db.execute(select(Task).order_by(Task.id.asc()))
    tasks = res.scalars().all()

    # Для ускорения — выберем прогресс по всем task_id сразу
    task_ids = [t.id for t in tasks]
    progress_map: Dict[int, TaskUserProgress] = {}
    if task_ids:
        res2 = await db.execute(
            select(TaskUserProgress).where(
                TaskUserProgress.telegram_id == telegram_id,
                TaskUserProgress.task_id.in_(task_ids),
            )
        )
        for p in res2.scalars().all():
            progress_map[p.task_id] = p

    items: List[Dict] = []
    for t in tasks:
        p = progress_map.get(t.id)
        status = p.status if p else "pending"
        completed = status in ("completed", "verified")
        items.append({
            "id": t.id,
            "code": t.code,
            "title": t.title,
            "reward": fmt_e(Decimal(t.reward_bonus_efhc or 0)),
            "completed": completed,
            "status": status,
        })
    return items


async def complete_task(db: AsyncSession, telegram_id: int, task_id: int) -> Dict[str, str]:
    """
    Помечает задание как выполненное пользователем и начисляет бонусные EFHC (если раньше не начисляли):
      • status: pending → completed → verified (в этой упрощенной версии сразу verified).
      • Баланс: bonus += reward_bonus_efhc.
    """
    # Найдём задание
    res = await db.execute(select(Task).where(Task.id == task_id))
    task = res.scalar_one_or_none()
    if task is None:
        raise RuntimeError("Задание не найдено.")

    # Прогресс пользователя по заданию
    res2 = await db.execute(select(TaskUserProgress).where(
        TaskUserProgress.task_id == task_id,
        TaskUserProgress.telegram_id == telegram_id
    ))
    prog = res2.scalar_one_or_none()

    # Баланс
    resb = await db.execute(select(Balance).where(Balance.telegram_id == telegram_id))
    bal = resb.scalar_one_or_none()
    if bal is None:
        raise RuntimeError("Баланс не найден.")

    # Если уже verified — повторно не начисляем
    if prog and prog.status == "verified":
        return {
            "status": "already_verified",
            "reward": fmt_e(Decimal(task.reward_bonus_efhc or 0)),
            "bonus": fmt_e(Decimal(bal.bonus or 0)),
        }

    # Обновляем/ставим прогресс в verified
    if prog is None:
        prog = TaskUserProgress(
            task_id=task_id,
            telegram_id=telegram_id,
            status="verified",
            updated_at=datetime.utcnow(),
        )
        db.add(prog)
    else:
        prog.status = "verified"
        prog.updated_at = datetime.utcnow()

    # Начисляем бонус
    reward = Decimal(task.reward_bonus_efhc or 0)
    new_bonus = (Decimal(bal.bonus or 0) + reward).quantize(EFHC_Q, rounding=ROUND_DOWN)
    bal.bonus = new_bonus

    await db.flush()

    return {
        "status": "verified",
        "reward": fmt_e(reward),
        "bonus": fmt_e(new_bonus),
    }


# =============================================================================
# Админ-проверка (whoami) — минимально рабочая и безопасная
# =============================================================================

async def is_admin(db: AsyncSession, telegram_id: int) -> bool:
    """
    Возвращает True, если у пользователя есть право на админ-панель.

    Текущая реализация (минимальная, но рабочая):
      • Главный админ: settings.ADMIN_TELEGRAM_ID
      • (Точка расширения) NFT-проверка через TonAPI:
           - нужно хранить адрес TON-кошелька пользователя и проверять наличие
             NFT из whitelist (AdminNFTWhitelist) или из коллекции VIP_NFT_COLLECTION.
        В этом MVP мы НЕ имеем привязки кошелька к пользователю, поэтому
        оставляем только проверку по ADMIN_TELEGRAM_ID.
    """
    if telegram_id == settings.ADMIN_TELEGRAM_ID:
        return True

    # Здесь можно расширить: если вы добавите модель привязки user <-> ton_wallet,
    # то:
    #   1) Получить wallet пользователя
    #   2) Через TonAPI проверить, какие NFT у него на кошельке
    #   3) Если среди них есть любой из AdminNFTWhitelist.nft_address, вернуть True
    #
    # Пример (псевдо):
    # user_wallet = await get_user_wallet(db, telegram_id)
    # nft_addresses = await fetch_user_nfts_from_tonapi(user_wallet)
    # whitelisted = await fetch_whitelist(db) -> set([...])
    # if whitelisted ∩ nft_addresses != ∅:
    #     return True

    return False


# =============================================================================
# VIP: назначение / проверка
# =============================================================================

async def grant_vip(db: AsyncSession, telegram_id: int, nft_address: Optional[str] = None) -> Dict[str, str]:
    """
    Выдаёт пользователю VIP-статус (например, после обработки платежа с memo «VIP NFT»).
    Если уже есть — возвращает без изменений.
    """
    res = await db.execute(select(UserVIP).where(UserVIP.telegram_id == telegram_id))
    vip = res.scalar_one_or_none()
    if vip:
        return {"status": "already_vip", "telegram_id": str(telegram_id)}

    vip = UserVIP(telegram_id=telegram_id, nft_address=nft_address or None, activated_at=datetime.utcnow())
    db.add(vip)
    await db.flush()
    return {"status": "granted", "telegram_id": str(telegram_id)}


async def has_vip(db: AsyncSession, telegram_id: int) -> bool:
    """
    Проверяет наличие VIP статуса у пользователя.
    """
    res = await db.execute(select(UserVIP).where(UserVIP.telegram_id == telegram_id))
    return res.scalar_one_or_none() is not None


# =============================================================================
# Ежедневные начисления — интерфейсы для scheduler.py
# =============================================================================

async def get_users_for_daily_run(db: AsyncSession) -> List[int]:
    """
    Возвращает список telegram_id пользователей, у которых есть х2 смысла считать:
      • есть активные панели (expires_at > now)
    (Оптимизация: можно выбирать только тех, у кого суммарное число панелей > 0)
    """
    now = datetime.utcnow()
    res = await db.execute(
        select(Panel.telegram_id)
        .where(
            or_(Panel.expires_at.is_(None), Panel.expires_at > now)
        )
        .group_by(Panel.telegram_id)
    )
    ids = [r[0] for r in res.all()]
    return ids


async def was_daily_processed(db: AsyncSession, telegram_id: int, d: date) -> bool:
    """
    Проверяет, было ли уже начисление для пользователя в заданную дату d.
    """
    res = await db.execute(
        select(func.count(DailyGenerationLog.id))
        .where(DailyGenerationLog.telegram_id == telegram_id, DailyGenerationLog.run_date == d)
    )
    return (int(res.scalar_one() or 0) > 0)


async def accrue_daily_for_user(db: AsyncSession, telegram_id: int, run_date: date) -> Optional[Decimal]:
    """
    Начисляет kWh пользователю за сутки run_date.
    Формула:
        generated = DAILY_GEN_BASE_KWH * active_panels_count * VIP_MULTIPLIER(if vip else 1.0)
    где VIP_MULTIPLIER = 1.07 (как согласовано в переписке).

    Возвращает начисленную величину (Decimal) или None, если начисление не выполнено (нет панелей).
    """
    # Не дублируем начисление
    if await was_daily_processed(db, telegram_id, run_date):
        return None

    # Считаем активные панели
    now = datetime.utcnow()
    res_panels = await db.execute(
        select(func.coalesce(func.sum(Panel.count), 0)).where(
            and_(
                Panel.telegram_id == telegram_id,
                or_(Panel.expires_at.is_(None), Panel.expires_at > now)
            )
        )
    )
    panels_count = int(res_panels.scalar_one() or 0)
    if panels_count <= 0:
        return None

    # Проверяем VIP
    vip = await has_vip(db, telegram_id)
    multiplier = Decimal(str(settings.VIP_MULTIPLIER)) if vip else Decimal("1.0")

    # Расчёт начисления
    base = Decimal(str(settings.DAILY_GEN_BASE_KWH))          # 0.598
    generated = (base * Decimal(panels_count) * multiplier).quantize(KWH_Q, rounding=ROUND_DOWN)

    # Записываем в баланс
    resb = await db.execute(select(Balance).where(Balance.telegram_id == telegram_id))
    bal = resb.scalar_one_or_none()
    if bal is None:
        # На всякий случай создадим
        bal = Balance(telegram_id=telegram_id, kwh=Decimal("0.000"))
        db.add(bal)
        await db.flush()

    bal.kwh = (Decimal(bal.kwh or 0) + generated).quantize(KWH_Q, rounding=ROUND_DOWN)

    # Лог начисления
    log = DailyGenerationLog(
        telegram_id=telegram_id,
        run_date=run_date,
        generated_kwh=generated,
        panels_count=panels_count,
        vip=vip,
        created_at=datetime.utcnow(),
    )
    db.add(log)

    await db.flush()
    return generated
