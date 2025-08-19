# 📂 backend/app/bot.py — Telegram-бот EFHC (меню, кнопки, интеграция с API)
# -----------------------------------------------------------------------------
# Что делает:
# 1) Поднимает экземпляр aiogram Bot/Dispatcher.
# 2) Реализует команду /start и главное меню.
# 3) Обрабатывает разделы: Баланс, Панели (покупка с комбинированным списанием),
#    Обменник (кВт → EFHC), Задания (список + выполнение), Рефералы, Розыгрыши.
# 4) Проверяет NFT-доступ к админ-панели (кнопка видна только при доступе).
# 5) Работает в режиме Webhook — url задаётся в .env (BASE_PUBLIC_URL + TELEGRAM_WEBHOOK_PATH).
#
# ВАЖНО:
# - Все конфиги берём из config.py (переменные окружения).
# - Вызовы к нашему API (FastAPI) делаем через httpx на URLs вида {BACKEND_BASE_URL}/api/*.
# - В проде BACKEND_BASE_URL должен указывать на Render/домен FastAPI.
# - В Vercel фронт (React), а бэкенд — Render/другой VPS. Webhook должен указывать на публичный URL Render.
# - Подсистема начисления: VIP/NFT +7% — учитывается на стороне API/интернал-сервисов (scheduler/ton_integration).
# -----------------------------------------------------------------------------

import asyncio
from decimal import Decimal
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, Update
)
from aiogram.filters import CommandStart, Command
import httpx

from .config import get_settings

# -----------------------------------------------------------------------------
# Инициализация конфигурации
# -----------------------------------------------------------------------------
settings = get_settings()

# -----------------------------------------------------------------------------
# Глобальные объекты aiogram (бот и диспетчер)
# -----------------------------------------------------------------------------
# Bot — сущность Telegram-бота с токеном.
# parse_mode="HTML" — для форматирования сообщений с <b></b>, <i></i>.
bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, parse_mode="HTML")

# Dispatcher — единая шина маршрутизации апдейтов/команд/кнопок к хэндлерам.
dp = Dispatcher()

# Router — модульная часть диспетчера (можно подключать много разных роутеров)
router = Router()
dp.include_router(router)

# -----------------------------------------------------------------------------
# Настройки HTTP-запросов к backend API
# -----------------------------------------------------------------------------
# Базовый адрес бэкенда:
#  - На проде: HTTPS-домен Render/сервер бэкенда (настраивается в .env BACKEND_BASE_URL).
#  - Локально: http://127.0.0.1:8000
BACKEND_BASE_URL = getattr(settings, "BACKEND_BASE_URL", "http://127.0.0.1:8000")

# Префикс API (обычно "/api"), также задаётся в настройках
API_PREFIX = settings.API_V1_STR if hasattr(settings, "API_V1_STR") else "/api"

# Полные пути к ЭНДПОИНТАМ пользователя
API_USER_REGISTER        = f"{BACKEND_BASE_URL}{API_PREFIX}/user/register"
API_USER_BALANCE         = f"{BACKEND_BASE_URL}{API_PREFIX}/user/balance"
API_USER_BUY_PANEL       = f"{BACKEND_BASE_URL}{API_PREFIX}/user/panels/buy"
API_USER_EXCHANGE        = f"{BACKEND_BASE_URL}{API_PREFIX}/user/exchange"
API_USER_TASKS           = f"{BACKEND_BASE_URL}{API_PREFIX}/user/tasks"
API_USER_TASK_COMPLETE   = f"{BACKEND_BASE_URL}{API_PREFIX}/user/tasks/complete"
API_USER_REFERRALS       = f"{BACKEND_BASE_URL}{API_PREFIX}/user/referrals"
API_USER_LOTTERIES       = f"{BACKEND_BASE_URL}{API_PREFIX}/user/lotteries"
API_USER_LOTTERY_BUY     = f"{BACKEND_BASE_URL}{API_PREFIX}/user/lottery/buy"
API_USER_WITHDRAW        = f"{BACKEND_BASE_URL}{API_PREFIX}/user/withdraw"  # (план) вывод EFHC — механизм согласуется

# Эндпоинт проверки адм. прав (по whitelist NFT) — реализован в admin_routes.py
API_ADMIN_WHOAMI         = f"{BACKEND_BASE_URL}{API_PREFIX}/admin/whoami"

# -----------------------------------------------------------------------------
# Вспомогательные HTTP функции — GET/POST с заголовком X-Telegram-Id
# -----------------------------------------------------------------------------
async def _api_get(url: str, x_tid: int, params: Optional[dict] = None) -> dict:
    """
    Выполняет GET-запрос к нашему backend API с заголовком X-Telegram-Id.

    :param url: Полный URL backend API.
    :param x_tid: Telegram ID пользователя (для идентификации в бэкенде).
    :param params: Необязательные query-параметры.
    :raises RuntimeError: Если вернулся HTTP >= 400 — текст ошибки возвращаем пользователю.
    :return: JSON-ответ от API (словарь).
    """
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, headers={"X-Telegram-Id": str(x_tid)}, params=params)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except Exception:
                detail = r.text
            raise RuntimeError(detail or f"HTTP {r.status_code}")
        return r.json()

async def _api_post(url: str, x_tid: int, payload: Optional[dict] = None) -> dict:
    """
    Выполняет POST-запрос к нашему backend API с заголовком X-Telegram-Id.

    :param url: Полный URL backend API.
    :param x_tid: Telegram ID пользователя.
    :param payload: JSON-данные запроса (dict).
    :raises RuntimeError: Если статус ответа >= 400.
    :return: JSON-ответ API (словарь).
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
# Хелперы для клавиатур
# -----------------------------------------------------------------------------
def main_menu(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """
    Главная клавиатура (ReplyKeyboardMarkup).
    Если пользователь — админ (по NFT), добавляем кнопку Админ-панели (WebApp).
    В нашем проекте: доступ к админке либо по NFT из whitelist (через /api/admin/whoami),
    либо по TELEGRAM_ID (суперадмин).
    """
    rows = [
        [KeyboardButton(text="⚡ Энергия"), KeyboardButton(text="🔁 Обменник")],
        [KeyboardButton(text="🔩 Панели"), KeyboardButton(text="🎟 Розыгрыши")],
        [KeyboardButton(text="📋 Задания"), KeyboardButton(text="👥 Рефералы")],
        [KeyboardButton(text="🏆 Рейтинг"), KeyboardButton(text="💼 Магазин")],
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
    Подменю «Обменник».
    Позволяет пользователю обменять доступный kWh → EFHC (по курсу 1:1).
    Также даём навигацию на Розыгрыши, Задания и Назад.
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
    Подменю «Панели». Покупка за 100 EFHC.
    Комбинированное списание: сначала бонусные EFHC, потом основной баланс.
    """
    rows = [
        [InlineKeyboardButton(text="Купить панель (100 EFHC)", callback_data="panels:buy")] if show_buy else [],
        [InlineKeyboardButton(text="Обменять бонусы на панель", callback_data="panels:buy_bonus")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")],
    ]
    # Фильтрация пустых строк
    rows = [r for r in rows if r]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def lotteries_menu() -> InlineKeyboardMarkup:
    """
    Подменю «Розыгрыши».
    Показывает кнопку «Обновить» и возврат назад (главное меню).
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить список", callback_data="lottery:list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")]
    ])

def tasks_menu() -> InlineKeyboardMarkup:
    """
    Подменю «Задания».
    Показывает «Обновить» и «Назад».
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обновить список", callback_data="tasks:list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")]
    ])

# -----------------------------------------------------------------------------
# Команда /start — регистрация и приветствие, проверка при наличии доступа в админку
# -----------------------------------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message):
    """
    Обработчик команды /start:
    - Регистрируем пользователя в нашей системе (идемпотентный вызов /user/register).
    - Проверяем, админ ли (по NFT whitelist или SUPER-ADMIN по TELEGRAM ID).
    - Выводим приветственное сообщение + главное меню (включая «🛠 Админ-панель», если доступ есть).
    """
    x_tid = message.from_user.id
    username = (message.from_user.username or "").strip()

    # Регистрируем пользователя (идемпотентный эндпоинт)
    try:
        await _api_post(API_USER_REGISTER, x_tid=x_tid, payload={"username": username})
    except Exception as e:
        await message.answer(f"❌ Ошибка регистрации: {e}")
        return

    # Проверка прав администратора (по NFT whitelist/SUPER-ADMIN)
    is_admin = False
    try:
        who = await _api_get(API_ADMIN_WHOAMI, x_tid=x_tid)
        is_admin = bool(who.get("is_admin", False))
    except Exception:
        # Молча игнорируем — кнопка не добавится, пользователь обычный
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
    """
    Раздел «Энергия» — отображает текущий баланс:
      - Основной EFHC,
      - Бонусные EFHC,
      - Киловатт-часы (kWh).
    Информация получается из /user/balance.
    """
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
        "ℹ️ Курс фиксированный: 1 кВт = 1 EFHC.\n"
        "🔹 VIP даёт +7% к ежедневной генерации (при наличии NFT VIP коллекции или внутреннего VIP)."
    )
    await message.answer(text)

@router.message(F.text == "🔁 Обменник")
async def on_exchange(message: Message):
    """
    Раздел «Обменник» — позволяет пользователю обменять kWh → EFHC (1:1).
    Вызываем /user/balance для отображения данных.
    """
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
        "Нажмите для обмена кВт → EFHC (1:1)."
    )
    await message.answer(text, reply_markup=exchange_menu())

@router.message(F.text == "🔩 Панели")
async def on_panels(message: Message):
    """
    Раздел «Панели» — информация по покупке панелей.
    Цена панели = 100 EFHC.
    При покупке сначала списываются бонусные EFHC, затем основной баланс (комбинированное списание).
    """
    x_tid = message.from_user.id
    try:
        b = await _api_get(API_USER_BALANCE, x_tid)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return

    text = (
        "🔩 <b>Панели</b>\n"
        f"Основной EFHC: <b>{b['efhc']}</b>\n"
        f"Бонусные EFHC: <b>{b['bonus']}</b>\n\n"
        "Цена панели = <b>100 EFHC</b>.\n"
        "💡 При покупке сначала списываются <b>бонусные</b> EFHC, затем — основной баланс.\n"
        "⛔ Ограничение: максимум <b>1000</b> панелей на пользователя."
    )
    await message.answer(text, reply_markup=panels_menu(show_buy=True))

@router.message(F.text == "🎟 Розыгрыши")
async def on_lotteries(message: Message):
    """
    Раздел «Розыгрыши» — выводим список активных лотерей (загружается с /user/lotteries).
    """
    await _send_lotteries_list(message.chat.id, message.from_user.id)

@router.message(F.text == "📋 Задания")
async def on_tasks(message: Message):
    """
    Раздел «Задания» — показываем список доступных задач (загружается из /user/tasks).
    Выплаты за задания начисляются в бонусные EFHC.
    """
    await _send_tasks_list(message.chat.id, message.from_user.id)

@router.message(F.text == "👥 Рефералы")
async def on_referrals(message: Message):
    """
    Раздел «Рефералы» — выводит статистику рефералов пользователя.
    - Список с флагом активности.
    - Зарегистрированная рефссылка доступна в WebApp (фронтенд).
    """
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

@router.message(F.text == "🏆 Рейтинг")
async def on_ranking(message: Message):
    """
    Раздел «Рейтинг» — (может быть реализован позже на фронтенде).
    По задумке: вывод таблицы лидеров по суммарной выработке kWh или по количеству панелей.
    Здесь только подсказка о наличии WebApp раздела Ranks.
    """
    wa = settings.TELEGRAM_WEBAPP_URL if hasattr(settings, "TELEGRAM_WEBAPP_URL") else None
    if wa:
        await message.answer(
            f"🏆 Раздел «Рейтинг» доступен в WebApp:\n{wa}/ranks\n\n"
            "Смотрите лидеров по выработке энергии."
        )
    else:
        await message.answer("🏆 Раздел «Рейтинг» доступен в WebApp. Установите TELEGRAM_WEBAPP_URL в .env")

@router.message(F.text == "💼 Магазин")
async def on_shop(message: Message):
    """
    Раздел «Магазин» — выполнен на фронтенде (React+Tailwind).
    Здесь даём ссылку на WebApp и краткое описание:
    - Покупка EFHC / VIP / панелей за TON/USDT/EFHC.
    - Начисления EFHC выполняются по факту входящего платежа через ton_integration.
    """
    wa = getattr(settings, "TELEGRAM_WEBAPP_URL", None)
    if wa:
        await message.answer(
            f"💼 Магазин открыт в WebApp:\n{wa}\n\n"
            "Оплачивайте TON/USDT/EFHC. Начисление EFHC выполняется внутренним переводом от админа (TonAPI watcher)."
        )
    else:
        await message.answer(
            "💼 Магазин доступен в WebApp. Установите TELEGRAM_WEBAPP_URL в .env, "
            "чтобы отправлять ссылку пользователю."
        )

@router.message(F.text == "🛠 Админ-панель")
async def on_admin(message: Message):
    """
    Раздел «Админ-панель» — проверяет право доступа через /api/admin/whoami:
      - Суперадмин (settings.ADMIN_TELEGRAM_ID),
      - Админ по NFT whitelist (таблица admin_nft_whitelist + TonAPI проверка).
    Если проверки пройдены, даём ссылку на WebApp админку.
    """
    x_tid = message.from_user.id
    try:
        who = await _api_get(API_ADMIN_WHOAMI, x_tid=x_tid)
        if not who.get("is_admin"):
            await message.answer("⛔ Доступ запрещён. Требуется админ NFT из whitelist или SUPER-ADMIN ID.")
            return
    except Exception as e:
        await message.answer(f"❌ Ошибка проверки прав: {e}")
        return

    wa = getattr(settings, "TELEGRAM_WEBAPP_URL", None)
    if wa:
        await message.answer(f"🛠 Админ-панель:\n{wa}/admin\n\n"
                             "• Управление товарами и начислениями EFHC (банк)\n"
                             "• Настройка whitelist NFT\n"
                             "• Просмотр логов TON событий\n"
                             "• Задания/Лотереи/Акции\n")
    else:
        await message.answer("🛠 Установите TELEGRAM_WEBAPP_URL в .env, чтобы открыть админ-панель (WebApp).")

# -----------------------------------------------------------------------------
# CallbackQuery: навигация и действия (Обменник, Панели, Розыгрыши, Задания)
# -----------------------------------------------------------------------------
@router.callback_query(F.data == "nav:home")
async def cb_nav_home(cq: CallbackQuery):
    """
    Callback «Назад» — возвращаемся в главное меню.
    Уточняем флаг is_admin через admin_whoami.
    """
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
    """
    Callback диспатчинг в «Розыгрыши» (с обновлением контента).
    """
    await _send_lotteries_list(cq.message.chat.id, cq.from_user.id, edit=True, cq=cq)

@router.callback_query(F.data == "nav:tasks")
async def cb_nav_tasks(cq: CallbackQuery):
    """
    Callback диспатчинг в «Задания» (с обновлением контента).
    """
    await _send_tasks_list(cq.message.chat.id, cq.from_user.id, edit=True, cq=cq)

# --- ОБМЕННИК -----------------------------------------------------------------
@router.callback_query(F.data == "ex:convert")
async def cb_exchange_convert(cq: CallbackQuery):
    """
    Нажатие на «Обменять кВт → EFHC (1:1)».
    Сценарий простой: меняем всё доступное kWh в EFHC.
    В будущем можно расширить на ввод суммы (FSM/стейт машины).
    """
    x_tid = cq.from_user.id
    try:
        b = await _api_get(API_USER_BALANCE, x_tid)
        kwh = Decimal(b["kwh"])
        if kwh <= Decimal("0.000"):
            await cq.answer("Недостаточно кВт для обмена.", show_alert=True)
            return
        # Меняем всё доступное (реализовано в user_routes)
        await _api_post(API_USER_EXCHANGE, x_tid, {"amount_kwh": str(kwh)})
    except Exception as e:
        await cq.answer(f"Ошибка: {e}", show_alert=True)
        return

    await cq.message.edit_text("✅ Обмен выполнен. КВт → EFHC (1:1).")
    await cq.message.answer("Возврат в Обменник:", reply_markup=exchange_menu())
    await cq.answer()

# --- ПАНЕЛИ -------------------------------------------------------------------
@router.callback_query(F.data == "panels:buy")
async def cb_panels_buy(cq: CallbackQuery):
    """
    Подготовка к покупке панели: показываем подтверждение с расчётом комбинированного списания
    бонусных и основных EFHC на цену 100.
    """
    x_tid = cq.from_user.id
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

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить покупку", callback_data="panels:confirm_buy")],
        [InlineKeyboardButton(text="◀️ Отмена", callback_data="nav:home")]
    ])
    text = (
        "Подтвердите покупку панели за <b>100 EFHC</b>.\n"
        f"Будет списано: <b>{min(bonus, price):.3f}</b> бонусных + <b>{max(Decimal('0.000'), price - bonus):.3f}</b> основных.\n"
        "🔹 VIP увеличивает ежедневную генерацию на +7%, если у вас есть NFT или внутренний VIP."
    )
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()

@router.callback_query(F.data == "panels:confirm_buy")
async def cb_panels_confirm_buy(cq: CallbackQuery):
    """
    Подтверждение покупки панели (с комбинированным списанием).
    Бизнес-логика реализована в /user/panels/buy — бэкенд посчитает bonus_used/main_used/лимиты.
    """
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
    просто подчёркиваем, что в первую очередь уйдут бонусные.
    Если бонусов < 100, остаток возьмётся из основного баланса.
    """
    await cb_panels_buy(cq)

# --- РОЗЫГРЫШИ ----------------------------------------------------------------
@router.callback_query(F.data == "lottery:list")
async def cb_lottery_list(cq: CallbackQuery):
    """
    Обновление списка лотерей по нажатию «Обновить» в разделе «Розыгрыши».
    """
    await _send_lotteries_list(cq.message.chat.id, cq.from_user.id, edit=True, cq=cq)

async def _send_lotteries_list(chat_id: int, x_tid: int, edit: bool = False, cq: Optional[CallbackQuery] = None):
    """
    Утилита для отображения списка лотерей:
    - Загружает массив активных лотерей через /user/lotteries.
    - Строит примитивный прогресс-блок по заполняемости.
    - Даёт кнопки купить 1/5/10 билетов.
    """
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
            target = l.get("target", 0) or l.get("target_participants", 0)
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
    """
    Покупка билетов — отправляем в /user/lottery/buy.
    Для простоты выбираем первую активную лотерею в списке (как в UI).
    В реальном приложении можно дать пользователю выбрать конкретную лотерею.
    """
    x_tid = cq.from_user.id
    count = int(cq.data.split(":")[-1])

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

# --- ЗАДАНИЯ ------------------------------------------------------------------
@router.callback_query(F.data == "tasks:list")
async def cb_tasks_list(cq: CallbackQuery):
    """
    Обновление списка заданий по нажатию «Обновить» в разделе «Задания».
    """
    await _send_tasks_list(cq.message.chat.id, cq.from_user.id, edit=True, cq=cq)

async def _send_tasks_list(chat_id: int, x_tid: int, edit: bool = False, cq: Optional[CallbackQuery] = None):
    """
    Утилита для вывода списка заданий:
    - Загружает через /user/tasks (включая флаг completed).
    - Также грузим баланс (/user/balance), чтобы показать текущие бонусные EFHC.
    """
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
    kb_rows.append([InlineKeyboardButton(text="Обновить", callback_data="tasks:list")])
    kb_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="nav:home")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if edit and cq:
        await cq.message.edit_text(text, reply_markup=kb)
        await cq.answer()
    else:
        await bot.send_message(chat_id, text, reply_markup=kb)

# -----------------------------------------------------------------------------
# Команды /help и /balance
# -----------------------------------------------------------------------------
@router.message(Command("help"))
async def cmd_help(message: Message):
    """
    Обработчик команды /help — показывает доступные команды.
    """
    await message.answer(
        "ℹ️ Доступные команды:\n"
        "/start — главное меню\n"
        "/balance — показать баланс\n"
        "/help — помощь"
    )

@router.message(Command("balance"))
async def cmd_balance(message: Message):
    """
    Обработчик команды /balance — выводит текущий баланс (как раздел «Энергия»).
    """
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
# Дополнительно: /withdraw — демонстрация вызова вывода EFHC (если будет реализован)
# -----------------------------------------------------------------------------
@router.message(Command("withdraw"))
async def cmd_withdraw(message: Message):
    """
    Обработчик команды /withdraw — вывод EFHC на внешний кошелёк.
    ⚠️ Внимание: реальная реализация вывода EFHC — политически чувствительная зона.
       Обычно это согласование с админом и проверка AML.
       В нашем случае предполагаем только формирование заявки в API,
       а админ выполняет вручную перевод токенов.

    Параметры сейчас не спрашиваются — вызываем stub (заглушку).
    """
    x_tid = message.from_user.id
    try:
        # В перспективе: запросить адрес и сумму у пользователя (FSM), здесь демонстрация
        res = await _api_post(API_USER_WITHDRAW, x_tid, payload={"amount": "10", "to_wallet": "EQ...your wallet..."})
        await message.answer(f"🔁 Заявка на вывод создана: {res}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# -----------------------------------------------------------------------------
# Регистрация команд бота (которые видны в меню Telegram "команды")
# -----------------------------------------------------------------------------
async def _set_bot_commands():
    """
    Устанавливает список команд бота, чтобы пользователи видели подсказки в Telegram UI.
    """
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="balance", description="Баланс (EFHC/kWh)"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="withdraw", description="Вывод EFHC (заявка)")
    ]
    await bot.set_my_commands(commands)

# -----------------------------------------------------------------------------
# Функции для webhook/polling запуска
# -----------------------------------------------------------------------------
async def setup_webhook():
    """
    Устанавливает webhook для бота.

    Используем настройки:
    - BASE_PUBLIC_URL — публичный домен сервера backend (Render/VPS)
    - TELEGRAM_WEBHOOK_PATH — путь, куда Telegram будет слать апдейты (например, "/tg/webhook")
    - TELEGRAM_WEBHOOK_SECRET — опциональный секрет. Telegram поддерживает параметр 'secret_token'.
      Мы передаём его в set_webhook, а сервер (main.py) проверяет заголовок "X-Telegram-Webhook-Secret".

    Поведение:
    - Если BASE_PUBLIC_URL не задан, логируем предупреждение и не назначаем webhook (подходит для локала).
    """
    base = getattr(settings, "BASE_PUBLIC_URL", None)
    path = getattr(settings, "TELEGRAM_WEBHOOK_PATH", "/tg/webhook")
    secret = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", None)

    if not base:
        print("[EFHC][BOT][WEBHOOK] BASE_PUBLIC_URL не задан. Webhook пропущен (локальная отладка?)")
        return

    webhook_url = f"{base.rstrip('/')}{path}"

    # Telegram Bot API v6+ поддерживает secret_token при установке webhook
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        ok = await bot.set_webhook(url=webhook_url, drop_pending_updates=True, secret_token=secret)
        print(f"[EFHC][BOT] Set webhook to: {webhook_url} (ok={ok}, secret={'yes' if secret else 'no'})")
    except Exception as e:
        print(f"[EFHC][BOT] Failed to set webhook: {e}")

async def start_bot():
    """
    Запуск бота в режиме polling (локальная разработка).
    - Вызывает _set_bot_commands()
    - Стартует dp.start_polling(bot)
    - Перехват ошибок polling'а
    """
    print("[EFHC][BOT] Starting polling...")
    await _set_bot_commands()
    try:
        await dp.start_polling(bot, allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"[EFHC][BOT] polling error: {e}", flush=True)

# -----------------------------------------------------------------------------
# Вспомогательная функция для обработки update из FastAPI (если захочется использовать напрямую)
# -----------------------------------------------------------------------------
async def handle_update(update: dict):
    """
    Универсальная функция прокидывания update в Dispatcher.
    Используется в старом варианте (через явный вызов),
    В текущей архитектуре основным способом является feed_raw_update в main.py/webhook.
    """
    try:
        await dp.feed_raw_update(bot, update)
    except Exception as e:
        print(f"[EFHC][BOT] handle_update error: {e}")
    return {"ok": True}

# -----------------------------------------------------------------------------
# Утилита получения Dispatcher для main.py
# -----------------------------------------------------------------------------
def get_dispatcher() -> Dispatcher:
    """
    Возвращает глобальный Dispatcher, чтобы main.py мог передать его в FastAPI webhook handler.
    """
    return dp
