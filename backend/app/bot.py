# 📂 backend/app/bot.py — Telegram-бот EFHC (меню, кнопки, интеграция с API)
# -----------------------------------------------------------------------------
# Этот модуль:
# 1) Поднимает экземпляр aiogram Bot/Dispatcher.
# 2) Реализует команду /start и главное меню.
# 3) Обрабатывает разделы: Баланс, Панели (покупка с комбинированным списанием),
#    Обменник (кВт → EFHC), Задания (список + выполнение), Рефералы, Розыгрыши.
# 4) Проверяет NFT-доступ к админ-панели (кнопка видна только при доступе).
# 5) Работает в режиме Webhook — url задаётся в .env (BOT_WEBHOOK_URL / BASE_PUBLIC_URL).
#
# ВАЖНО:
# - Все конфиги берём из config.py (переменные окружения).
# - Вызовы к нашему API (FastAPI) делаем через httpx на URLs вида {BACKEND_BASE_URL}/api/*.
# - В проде BACKEND_BASE_URL должен указывать на Render/домен FastAPI.
# - Если FastAPI и бот живут в одном процессе (как у нас), можно использовать 127.0.0.1:порт.
# - В Vercel фронт, а бэкенд — Render/другой VPS. Webhook должен указывать на публичный URL Render.
# -----------------------------------------------------------------------------

import asyncio
from decimal import Decimal
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import CommandStart, Command
import httpx

from .config import get_settings

settings = get_settings()

# -----------------------------------------------------------------------------
# Глобальные объекты aiogram
# -----------------------------------------------------------------------------
bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()
router = Router()
dp.include_router(router)

# -----------------------------------------------------------------------------
# Настройки адресов API
# -----------------------------------------------------------------------------
# Мы используем единый префикс API (например "/api").
# Базовый адрес бэкенда:
# - На проде: HTTPS-домен Render/сервер бэкенда. Установить в .env BACKEND_BASE_URL.
# - Локально: http://127.0.0.1:8000
BACKEND_BASE_URL = getattr(settings, "BACKEND_BASE_URL", "http://127.0.0.1:8000")
API_PREFIX = settings.API_V1_STR if hasattr(settings, "API_V1_STR") else "/api"

# Полные пути:
API_USER_REGISTER = f"{BACKEND_BASE_URL}{API_PREFIX}/user/register"
API_USER_BALANCE = f"{BACKEND_BASE_URL}{API_PREFIX}/user/balance"
API_USER_BUY_PANEL = f"{BACKEND_BASE_URL}{API_PREFIX}/user/panels/buy"
API_USER_EXCHANGE = f"{BACKEND_BASE_URL}{API_PREFIX}/user/exchange"
API_USER_TASKS = f"{BACKEND_BASE_URL}{API_PREFIX}/user/tasks"
API_USER_TASK_COMPLETE = f"{BACKEND_BASE_URL}{API_PREFIX}/user/tasks/complete"
API_USER_REFERRALS = f"{BACKEND_BASE_URL}{API_PREFIX}/user/referrals"
API_USER_LOTTERIES = f"{BACKEND_BASE_URL}{API_PREFIX}/user/lotteries"
API_USER_LOTTERY_BUY = f"{BACKEND_BASE_URL}{API_PREFIX}/user/lottery/buy"

# Эндпоинт проверки адм. прав (по whitelist NFT) — должен быть реализован в admin_routes.py
API_ADMIN_WHOAMI = f"{BACKEND_BASE_URL}{API_PREFIX}/admin/whoami"

# -----------------------------------------------------------------------------
# Вспомогательные функции HTTP
# -----------------------------------------------------------------------------
async def _api_get(url: str, x_tid: int, params: Optional[dict] = None):
    """
    GET к нашему API с передачей заголовка X-Telegram-Id (обязателен).
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers={"X-Telegram-Id": str(x_tid)}, params=params)
        if r.status_code >= 400:
            # Вернём понятную ошибку пользователю
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text
            raise RuntimeError(detail or f"HTTP {r.status_code}")
        return r.json()

async def _api_post(url: str, x_tid: int, payload: Optional[dict] = None):
    """
    POST к нашему API с передачей заголовка X-Telegram-Id (обязателен).
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, headers={"X-Telegram-Id": str(x_tid)}, json=payload or {})
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text
            raise RuntimeError(detail or f"HTTP {r.status_code}")
        return r.json()

# -----------------------------------------------------------------------------
# Клавиатуры
# -----------------------------------------------------------------------------
def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """
    Главное меню.
    Если пользователь — админ (по NFT), добавляем кнопку Админ-панели (WebApp).
    """
    rows = [
        [KeyboardButton(text="⚡ Энергия"), KeyboardButton(text="🔁 Обменник")],
        [KeyboardButton(text="🔩 Панели"), KeyboardButton(text="🎟 Розыгрыши")],
        [KeyboardButton(text="📋 Задания"), KeyboardButton(text="👥 Рефералы")],
        [KeyboardButton(text="💼 Магазин")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🛠 Админ-панель")])

    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите раздел…"
    )

def exchange_menu() -> InlineKeyboardMarkup:
    """
    Подменю раздела «Обменник».
    """
    kb = [
        [InlineKeyboardButton(text="Обменять кВт → EFHC (1:1)", callback_data="ex:convert")],
        [InlineKeyboardButton(text="🎲 Розыгрыши", callback_data="nav:lotteries")],
        [InlineKeyboardButton(text="📋 Задания", callback_data="nav:tasks")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def panels_menu(show_buy: bool = True) -> InlineKeyboardMarkup:
    """
    Подменю раздела «Панели».
    """
    rows = [
        [InlineKeyboardButton(text="Купить панель (100 EFHC)", callback_data="panels:buy")] if show_buy else [],
        [InlineKeyboardButton(text="Обменять бонусы на панель", callback_data="panels:buy_bonus")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")],
    ]
    # фильтруем пустые
    rows = [r for r in rows if r]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def lotteries_menu() -> InlineKeyboardMarkup:
    """
    Подменю «Розыгрыши».
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить список", callback_data="lottery:list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")]
    ])

def tasks_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить список", callback_data="tasks:list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")]
    ])

# -----------------------------------------------------------------------------
# Команда /start — регистрация и приветствие
# -----------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message):
    x_tid = message.from_user.id
    username = (message.from_user.username or "").strip()

    # Регистрируем пользователя (idempotent)
    try:
        await _api_post(API_USER_REGISTER, x_tid=x_tid, payload={"username": username})
    except Exception as e:
        await message.answer(f"❌ Ошибка регистрации: {e}")
        return

    # Проверка, админ ли пользователь (по NFT white-list)
    is_admin = False
    try:
        who = await _api_get(API_ADMIN_WHOAMI, x_tid=x_tid)
        is_admin = bool(who.get("is_admin", False))
    except Exception:
        # Молча игнорируем — кнопка не будет показана
        pass

    text = (
        "👋 Добро пожаловать в <b>EFHC</b>!\n\n"
        "Здесь вы можете:\n"
        "• Управлять панелями и генерацией энергии\n"
        "• Обменивать кВт → EFHC (1:1)\n"
        "• Участвовать в розыгрышах и выполнять задания\n"
        "• Получать бонусы и звать друзей по реферальной ссылке\n\n"
        "Выберите раздел ниже."
    )
    await message.answer(text, reply_markup=main_menu(is_admin=is_admin))

# -----------------------------------------------------------------------------
# Главное меню — текстовые кнопки
# -----------------------------------------------------------------------------
@router.message(F.text == "⚡ Энергия")
async def on_energy(message: Message):
    x_tid = message.from_user.id
    # Баланс из API
    try:
        b = await _api_get(API_USER_BALANCE, x_tid)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    text = (
        f"⚡ <b>Ваш баланс</b>\n"
        f"EFHC: <b>{b['efhc']}</b>\n"
        f"Бонусные EFHC: <b>{b['bonus']}</b>\n"
        f"Киловатт-часы: <b>{b['kwh']}</b>\n\n"
        "⚠️ Курс фиксированный: 1 кВт = 1 EFHC."
    )
    await message.answer(text)

@router.message(F.text == "🔁 Обменник")
async def on_exchange(message: Message):
    x_tid = message.from_user.id
    try:
        b = await _api_get(API_USER_BALANCE, x_tid)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    text = (
        "🔁 <b>Обменник</b>\n"
        f"КВт доступно: <b>{b['kwh']}</b>\n"
        f"EFHC: <b>{b['efhc']}</b>\n\n"
        "Выберите действие:"
    )
    await message.answer(text, reply_markup=exchange_menu())

@router.message(F.text == "🔩 Панели")
async def on_panels(message: Message):
    x_tid = message.from_user.id
    try:
        b = await _api_get(API_USER_BALANCE, x_tid)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    # Подсветка раздельного списания
    text = (
        "🔩 <b>Панели</b>\n"
        f"Основной EFHC: <b>{b['efhc']}</b>\n"
        f"Бонусные EFHC: <b>{b['bonus']}</b>\n\n"
        "Цена панели = <b>100 EFHC</b>.\n"
        "При покупке сначала списываются <b>бонусные</b> EFHC, затем — основной баланс."
    )
    await message.answer(text, reply_markup=panels_menu(show_buy=True))

@router.message(F.text == "🎟 Розыгрыши")
async def on_lotteries(message: Message):
    await _send_lotteries_list(message.chat.id, message.from_user.id)

@router.message(F.text == "📋 Задания")
async def on_tasks(message: Message):
    await _send_tasks_list(message.chat.id, message.from_user.id)

@router.message(F.text == "👥 Рефералы")
async def on_referrals(message: Message):
    x_tid = message.from_user.id
    try:
        refs = await _api_get(API_USER_REFERRALS, x_tid)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    if not refs:
        await message.answer("Пока нет рефералов.")
        return

    active = sum(1 for r in refs if r.get("active"))
    text = (
        f"👥 <b>Ваши рефералы</b>\n"
        f"Всего: <b>{len(refs)}</b>\n"
        f"Активных: <b>{active}</b>\n\n"
        "Реферальную ссылку выдаёт фронтенд WebApp (кнопка «Поделиться»)."
    )
    await message.answer(text)

@router.message(F.text == "💼 Магазин")
async def on_shop(message: Message):
    # В текущей версии покупки EFHC/VIP/и т.д. оформляются через WebApp (фронт).
    # Здесь даём подсказку и ссылку на WebApp, если задан TELEGRAM_WEBAPP_URL
    wa = settings.TELEGRAM_WEBAPP_URL
    if wa:
        await message.answer(
            f"💼 Магазин открыт в WebApp:\n{wa}\n\n"
            "Оплачивайте TON/USDT/EFHC. Начисление EFHC выполняется внутренним переводом от админа."
        )
    else:
        await message.answer(
            "💼 Магазин доступен в WebApp. Установите TELEGRAM_WEBAPP_URL в .env, "
            "чтобы отправлять ссылку пользователю."
        )

@router.message(F.text == "🛠 Админ-панель")
async def on_admin(message: Message):
    x_tid = message.from_user.id
    try:
        who = await _api_get(API_ADMIN_WHOAMI, x_tid)
        if not who.get("is_admin"):
            await message.answer("⛔ Доступ запрещён. Требуется админ NFT из whitelist.")
            return
    except Exception as e:
        await message.answer(f"❌ Ошибка проверки прав: {e}")
        return

    # Встроенная WebApp-админка фронта (один и тот же WebApp, но открывает /admin)
    wa = settings.TELEGRAM_WEBAPP_URL
    if wa:
        await message.answer(f"🛠 Админ-панель:\n{wa}/admin")
    else:
        await message.answer("🛠 Установите TELEGRAM_WEBAPP_URL, чтобы открыть админ-панель (WebApp).")

# -----------------------------------------------------------------------------
# CallbackQuery: навигация и действия
# -----------------------------------------------------------------------------
@router.callback_query(F.data == "nav:home")
async def cb_nav_home(cq: CallbackQuery):
    # Обновим флаг is_admin, чтобы главная клавиатура была актуальна
    is_admin = False
    try:
        who = await _api_get(API_ADMIN_WHOAMI, x_tid=cq.from_user.id)
        is_admin = bool(who.get("is_admin"))
    except Exception:
        pass
    await cq.message.edit_text("Главное меню. Выберите раздел ниже.")
    await cq.message.answer("⬇️", reply_markup=main_menu(is_admin=is_admin))
    await cq.answer()

@router.callback_query(F.data == "nav:lotteries")
async def cb_nav_lotteries(cq: CallbackQuery):
    await _send_lotteries_list(cq.message.chat.id, cq.from_user.id, edit=True, cq=cq)

@router.callback_query(F.data == "nav:tasks")
async def cb_nav_tasks(cq: CallbackQuery):
    await _send_tasks_list(cq.message.chat.id, cq.from_user.id, edit=True, cq=cq)

# --- ОБМЕННИК ---
@router.callback_query(F.data == "ex:convert")
async def cb_exchange_convert(cq: CallbackQuery):
    """
    Простой сценарий: пробуем обменять весь доступный kWh в EFHC (1:1).
    Если нужно — можно сделать ввод суммы через Bot API (next step/FSM).
    """
    x_tid = cq.from_user.id
    # узнаем баланс
    try:
        b = await _api_get(API_USER_BALANCE, x_tid)
        kwh = Decimal(b["kwh"])
        if kwh <= Decimal("0.000"):
            await cq.answer("Недостаточно кВт для обмена.", show_alert=True)
            return
        # меняем всё
        await _api_post(API_USER_EXCHANGE, x_tid, {"amount_kwh": str(kwh)})
    except Exception as e:
        await cq.answer(f"Ошибка: {e}", show_alert=True)
        return

    await cq.message.edit_text("✅ Обмен выполнен. КВт → EFHC (1:1).")
    await cq.message.answer("Возврат в Обменник:", reply_markup=exchange_menu())
    await cq.answer()

# --- ПАНЕЛИ ---
@router.callback_query(F.data == "panels:buy")
async def cb_panels_buy(cq: CallbackQuery):
    """
    Покупка панели с комбинированным списанием:
    - сначала бонусные EFHC,
    - затем основной баланс.
    В подтверждении отображаем, сколько списалось откуда.
    """
    x_tid = cq.from_user.id
    # Получим баланс, чтобы заранее показать, сколько спишется
    try:
        b = await _api_get(API_USER_BALANCE, x_tid)
        bonus = Decimal(b["bonus"])
        efhc = Decimal(b["efhc"])
        price = Decimal("100.000")
        if bonus + efhc < price:
            await cq.answer(
                f"Недостаточно средств. Нужно 100 EFHC. У вас {bonus + efhc:.3f} (бонус {bonus:.3f} + основной {efhc:.3f}).",
                show_alert=True
            )
            return
    except Exception as e:
        await cq.answer(f"Ошибка: {e}", show_alert=True)
        return

    # Подтверждение (inline-кнопками)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить покупку", callback_data="panels:confirm_buy")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="nav:home")]
    ])
    text = (
        "Подтвердите покупку панели за <b>100 EFHC</b>.\n"
        f"Будет списано: <b>{min(bonus, price):.3f}</b> бонусных + <b>{max(Decimal('0.000'), price - bonus):.3f}</b> основных."
    )
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()

@router.callback_query(F.data == "panels:confirm_buy")
async def cb_panels_confirm_buy(cq: CallbackQuery):
    x_tid = cq.from_user.id
    try:
        res = await _api_post(API_USER_BUY_PANEL, x_tid)
        bonus_used = res.get("bonus_used", "0.000")
        main_used = res.get("main_used", "0.000")
        await cq.message.edit_text(
            f"✅ Панель куплена.\nСписано: <b>{bonus_used}</b> бонусных EFHC и <b>{main_used}</b> основных EFHC."
        )
        await cq.message.answer("Возврат к панелям:", reply_markup=panels_menu())
    except Exception as e:
        await cq.answer(f"Ошибка: {e}", show_alert=True)
    finally:
        await cq.answer()

@router.callback_query(F.data == "panels:buy_bonus")
async def cb_panels_buy_by_bonus(cq: CallbackQuery):
    """
    Кнопка «Обменять бонусы на панель» — фактически та же покупка (100 EFHC),
    просто подчеркиваем, что сначала уйдут бонусы. Если бонусов < 100, доберём из основного.
    """
    await cb_panels_buy(cq)

# --- РОЗЫГРЫШИ ---
@router.callback_query(F.data == "lottery:list")
async def cb_lottery_list(cq: CallbackQuery):
    await _send_lotteries_list(cq.message.chat.id, cq.from_user.id, edit=True, cq=cq)

async def _send_lotteries_list(chat_id: int, x_tid: int, edit: bool = False, cq: Optional[CallbackQuery] = None):
    try:
        lots = await _api_get(API_USER_LOTTERIES, x_tid)
    except Exception as e:
        if edit and cq:
            await cq.message.edit_text(f"❌ Ошибка: {e}", reply_markup=lotteries_menu())
            await cq.answer()
        else:
            await bot.send_message(chat_id, f"❌ Ошибка: {e}")
        return

    if not lots:
        text = "🎟 Активных розыгрышей сейчас нет."
    else:
        lines = ["🎟 <b>Активные розыгрыши</b>"]
        for l in lots:
            target = l.get("target", 0)
            sold = l.get("tickets_sold", 0)
            # Простейший «прогресс-бар» символами
            bar_len = 20
            filled = max(0, min(bar_len, int((sold / max(1, target)) * bar_len)))
            bar = "■" * filled + "□" * (bar_len - filled)
            lines.append(f"• {l['title']} — {sold}/{target}\n[{bar}]")
        lines.append("\nКупить билеты: по 1 EFHC, максимум 10 шт за раз.")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить 1 билет", callback_data="lottery:buy:1"),
         InlineKeyboardButton(text="Купить 5 билетов", callback_data="lottery:buy:5"),
         InlineKeyboardButton(text="Купить 10 билетов", callback_data="lottery:buy:10")],
        [InlineKeyboardButton(text="Обновить", callback_data="lottery:list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")]
    ])

    if edit and cq:
        await cq.message.edit_text(text, reply_markup=kb)
        await cq.answer()
    else:
        await bot.send_message(chat_id, text, reply_markup=kb)

@router.callback_query(F.data.startswith("lottery:buy:"))
async def cb_lottery_buy(cq: CallbackQuery):
    x_tid = cq.from_user.id
    count = int(cq.data.split(":")[-1])

    # Выбираем первую активную лотерею — в реальном UI можно дать список на выбор
    try:
        lots = await _api_get(API_USER_LOTTERIES, x_tid)
        if not lots:
            await cq.answer("Нет активных розыгрышей.", show_alert=True)
            return
        lottery_id = lots[0]["id"]
        await _api_post(API_USER_LOTTERY_BUY, x_tid, {"lottery_id": lottery_id, "count": count})
        await cq.answer("✅ Билеты куплены!", show_alert=True)
        await _send_lotteries_list(cq.message.chat.id, x_tid, edit=True, cq=cq)
    except Exception as e:
        await cq.answer(f"Ошибка: {e}", show_alert=True)

# --- ЗАДАНИЯ ---
@router.callback_query(F.data == "tasks:list")
async def cb_tasks_list(cq: CallbackQuery):
    await _send_tasks_list(cq.message.chat.id, cq.from_user.id, edit=True, cq=cq)

async def _send_tasks_list(chat_id: int, x_tid: int, edit: bool = False, cq: Optional[CallbackQuery] = None):
    try:
        tasks = await _api_get(API_USER_TASKS, x_tid)
        b = await _api_get(API_USER_BALANCE, x_tid)
    except Exception as e:
        if edit and cq:
            await cq.message.edit_text(f"❌ Ошибка: {e}", reply_markup=tasks_menu())
            await cq.answer()
        else:
            await bot.send_message(chat_id, f"❌ Ошибка: {e}")
        return

    lines = [
        "📋 <b>Задания</b>",
        f"Ваши бонусные EFHC: <b>{b['bonus']}</b>\n"
        "Выполняйте задания и обменивайте 100 бонусных EFHC на панель."
    ]
    if not tasks:
        lines.append("Пока заданий нет.")
    else:
        for t in tasks:
            status = "✅ Выполнено" if t["completed"] else "🟡 Доступно"
            url = t.get("url") or "—"
            lines.append(f"• {t['title']} (+{t['reward']} бонусных). {status}\n{url}")

    text = "\n".join(lines)
    kb_rows = []
    # Кнопки «Выполнить» формируем на стороне фронта; здесь показываем обновление списка.
    kb_rows.append([InlineKeyboardButton(text="Обновить", callback_data="tasks:list")])
    kb_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if edit and cq:
        await cq.message.edit_text(text, reply_markup=kb)
        await cq.answer()
    else:
        await bot.send_message(chat_id, text, reply_markup=kb)

# -----------------------------------------------------------------------------
# Команды /help и /balance (дополнительно)
# -----------------------------------------------------------------------------
@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ Доступные команды:\n"
        "/start — главное меню\n"
        "/balance — показать баланс\n"
        "/help — помощь"
    )

@router.message(Command("balance"))
async def cmd_balance(message: Message):
    x_tid = message.from_user.id
    try:
        b = await _api_get(API_USER_BALANCE, x_tid)
        await message.answer(
            f"EFHC: <b>{b['efhc']}</b>\n"
            f"Бонусные EFHC: <b>{b['bonus']}</b>\n"
            f"КВт: <b>{b['kwh']}</b>"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# -----------------------------------------------------------------------------
# Функции для webhook-настройки
# -----------------------------------------------------------------------------
async def setup_webhook():
    """
    Устанавливает webhook у бота.
    Требуется переменная окружения BASE_PUBLIC_URL или BOT_WEBHOOK_URL.
    - BASE_PUBLIC_URL — публичный домен вашего бэкенда, например:
      https://efhc-backend.onrender.com
    - BOT_WEBHOOK_URL — конкретный путь вебхука, если хотите переопределить.
    Пример конечного URL: {BASE_PUBLIC_URL}/tg-webhook
    """
    base = getattr(settings, "BASE_PUBLIC_URL", None)
    explicit = getattr(settings, "BOT_WEBHOOK_URL", None)
    if explicit:
        webhook_url = explicit
    elif base:
        webhook_url = f"{base.rstrip('/')}/tg-webhook"
    else:
        # Если нет публичного URL — логируем предупреждение (для локальной отладки можно использовать polling)
        print("[EFHC][WARN] BASE_PUBLIC_URL / BOT_WEBHOOK_URL не заданы. Webhook не будет установлен.")
        return

    await bot.delete_webhook(drop_pending_updates=True)
    ok = await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
    print(f"[EFHC][BOT] Set webhook to: {webhook_url} (ok={ok})")

def get_dispatcher() -> Dispatcher:
    """
    Возвращает Dispatcher, чтобы main.py мог передать его в FastAPI webhook handler.
    """
    return dp

