import React, { useEffect, useMemo, useState } from "react";
import axios from "axios";

/**
 * Panels.jsx — Страница управления панелями пользователя.
 * -----------------------------------------------------------------------------
 * Что делает:
 *  • Показывает:
 *      - Баланс EFHC (локальное отображение),
 *      - Суммарную генерацию в сутки,
 *      - Кол-во активных панелей,
 *      - Статусы: VIP и "Активный пользователь".
 *  • Отображает списки:
 *      - Активные панели (с днями до деактивации из 180),
 *      - Архивные панели (days_left = 0).
 *  • Реализует покупку панели за 100 EFHC с ограничением максимум 1000 активных панелей.
 *  • Обрабатывает состояние интерфейса: загрузка, ошибки, результат покупки.
 *
 * Зависимости:
 *  • axios — запросы к backend API:
 *      - GET /api/panels/:userId — получение данных панелей
 *      - POST /api/panels/purchase — покупка панели
 *
 * Важные константы (соответствуют бизнес-логике проекта):
 *  • PANEL_PRICE_EFHC = 100 (стоимость панели — всегда 100 EFHC)
 *  • MAX_ACTIVE_PANELS = 1000 (лимит одновременных активных панелей)
 *  • DAYS_ACTIVE = 180 (срок действия)
 *  • ENERGY_PER_DAY_BASE = 0.598 (без VIP)
 *  • ENERGY_PER_DAY_VIP = 0.640 (с VIP +7%)
 *
 * UI-компонент НЕ содержит верхней панели (TopBar). TopBar вынесен отдельно.
 *
 * Примечания:
 *  • Использует тёмную тему (black / gray / orange).
 *  • Статусы VIP и "Активный" отображаются как метки.
 *  • При достижении лимита 1000 активных панелей кнопка покупки блокируется.
 *  • При недостатке EFHC (<100) — также блокируется.
 *  • После покупки автоматически выполняется перезагрузка из бэкенда.
 */

const PANEL_PRICE_EFHC = 100;       // цена панели в EFHC (фиксированная)
const MAX_ACTIVE_PANELS = 1000;     // лимит активных панелей
const DAYS_ACTIVE = 180;            // срок действия панели в днях
const ENERGY_PER_DAY_BASE = 0.598;  // базовая генерация (кВт/сутки)
const ENERGY_PER_DAY_VIP = 0.640;   // VIP генерация (+7%, округлено 0.64)

export default function Panels({ userId }) {
  // --- Состояние данных, загруженных с бэка
  const [loading, setLoading] = useState(true);          // загрузка данных
  const [error, setError] = useState(null);              // текст ошибки
  const [activePanels, setActivePanels] = useState([]);  // список активных панелей
  const [archivedPanels, setArchivedPanels] = useState([]); // список архивных панелей
  const [isVip, setIsVip] = useState(false);             // VIP/NFT флаг
  const [isActiveUser, setIsActiveUser] = useState(false); // флаг активности пользователя
  const [genPerDay, setGenPerDay] = useState(0);         // суммарная генерация (кВт/сутки)
  const [panelCount, setPanelCount] = useState(0);       // кол-во активных панелей
  const [efhcBalance, setEfhcBalance] = useState(0);     // баланс EFHC для отображения

  // --- Состояние процесса покупки
  const [purchaseLoading, setPurchaseLoading] = useState(false);
  const [purchaseMessage, setPurchaseMessage] = useState("");

  /**
   * fetchPanelsData — загрузка данных панели с бекенда.
   * GET /api/panels/:userId
   *
   * Ответ (ожидаемый):
   * {
   *   "active_panels": [{"start_date": "2025-07-10", "days_left": 173}, ...],
   *   "archived_panels": [{"start_date": "2025-02-13", "days_left": 0}, ...],
   *   "is_vip": true,
   *   "is_active_user": true,
   *   "generation_per_day": 12.8,
   *   "panels_active_count": 20,
   *   "efhc_balance": 540.0
   * }
   */
  async function fetchPanelsData() {
    try {
      setLoading(true);
      setError(null);

      const res = await axios.get(`/api/panels/${userId}`);

      const data = res.data || {};
      setActivePanels(Array.isArray(data.active_panels) ? data.active_panels : []);
      setArchivedPanels(Array.isArray(data.archived_panels) ? data.archived_panels : []);
      setIsVip(Boolean(data.is_vip));
      setIsActiveUser(Boolean(data.is_active_user));
      setGenPerDay(Number(data.generation_per_day || 0));
      setPanelCount(Number(data.panels_active_count || 0));
      // Предпочтительно, чтобы бэк возвращал баланс EFHC:
      if (typeof data.efhc_balance === "number") {
        setEfhcBalance(Number(data.efhc_balance));
      }
      setLoading(false);
    } catch (err) {
      console.error("Ошибка загрузки панелей:", err);
      setError("Не удалось загрузить данные панелей. Повторите попытку позже.");
      setLoading(false);
    }
  }

  // Первичная загрузка
  useEffect(() => {
    fetchPanelsData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  /**
   * canPurchase — доступность покупки:
   *  • EFHC balance >= 100
   *  • panelCount < 1000
   */
  const canPurchase = useMemo(() => {
    return efhcBalance >= PANEL_PRICE_EFHC && panelCount < MAX_ACTIVE_PANELS;
  }, [efhcBalance, panelCount]);

  /**
   * handlePurchasePanel — действие "Купить панель".
   * POST /api/panels/purchase { user_id: number }
   *
   * Серверная логика:
   *  • Проверяет efhc_balance >= 100
   *  • Проверяет panelCount < 1000
   *  • Списывает 100 EFHC, добавляет 100EFHC в админский банк EFHC
   *  • Создаёт панель c 180 днями
   *  • Ставит is_active_user=true (если первой покупкой)
   */
  async function handlePurchasePanel() {
    setPurchaseMessage("");
    if (!canPurchase) {
      setPurchaseMessage("Недостаточно EFHC или достигнут лимит 1000 панелей.");
      return;
    }

    try {
      setPurchaseLoading(true);
      const resp = await axios.post("/api/panels/purchase", { user_id: userId });

      const ok = resp?.data?.success;
      const msg = resp?.data?.message || (ok ? "Панель успешно куплена." : "Не удалось купить панель.");

      setPurchaseMessage(msg);
      setPurchaseLoading(false);

      if (ok) {
        // Перезагрузка данных — обновить баланс, panelCount и списки
        await fetchPanelsData();
      }
    } catch (err) {
      console.error("Ошибка при покупке панели:", err);
      setPurchaseMessage("Ошибка при покупке. Повторите попытку позже.");
      setPurchaseLoading(false);
    }
  }

  if (loading) {
    return <div className="text-center text-white py-20">Загрузка панелей...</div>;
  }

  if (error) {
    return (
      <div className="text-center text-red-400 py-20">
        {error}
      </div>
    );
  }

  // UI: метка VIP/NFT
  const vipLabel = isVip ? (
    <span className="inline-block bg-green-600 text-white text-xs px-2 py-1 rounded-md">🌟 VIP</span>
  ) : (
    <span className="inline-block bg-gray-600 text-white text-xs px-2 py-1 rounded-md">🌟 VIP: нет</span>
  );

  // UI: метка "Активный пользователь"
  const activeUserLabel = isActiveUser ? (
    <span className="inline-block bg-green-500 text-white text-xs px-2 py-1 rounded-md">🟢 Активный</span>
  ) : (
    <span className="inline-block bg-gray-600 text-white text-xs px-2 py-1 rounded-md">🔴 Не активный</span>
  );

  return (
    <div className="w-full min-h-screen bg-black text-white px-4 py-6">
      {/* Верхний блок с цифрами и статусами (локальный для страницы Panels) */}
      <div className="mb-4 p-4 rounded-xl bg-gray-900">
        {/* Строка: Баланс EFHC и Генерация/сутки */}
        <div className="flex items-center justify-between mb-2">
          <div>
            <div className="text-xs text-gray-400">Баланс EFHC</div>
            <div className="text-2xl font-semibold">{Number(efhcBalance).toFixed(3)} EFHC</div>
          </div>
          <div className="text-right">
            <div className="text-xs text-gray-400">Генерация / сутки</div>
            <div className="text-2xl font-semibold">{Number(genPerDay).toFixed(3)} кВт</div>
          </div>
        </div>
        {/* Строка: Кол-во панелей и статусы */}
        <div className="flex items-center justify-between">
          <div>
            <div className="text-xs text-gray-400">Активных панелей</div>
            <div className="text-2xl font-bold">{panelCount}</div>
            <div className="text-xs text-gray-500">Максимум: {MAX_ACTIVE_PANELS}</div>
          </div>
          <div className="flex gap-2">
            {vipLabel}
            {activeUserLabel}
          </div>
        </div>
      </div>

      {/* Кнопка покупки панели */}
      <div className="mb-4">
        <button
          onClick={handlePurchasePanel}
          disabled={!canPurchase || purchaseLoading}
          className={`w-full py-3 rounded-xl text-white font-semibold
            ${canPurchase && !purchaseLoading ? "bg-orange-600 hover:bg-orange-500" : "bg-gray-700 cursor-not-allowed"}
          `}
        >
          {purchaseLoading ? "Покупка..." : `Купить панель — ${PANEL_PRICE_EFHC} EFHC`}
        </button>
        <p className="text-xs text-gray-400 mt-2">
          Стоимость панели фиксированная: 100 EFHC. Лимит активных панелей: 1000. Панель активна 180 дней.
        </p>
        {purchaseMessage && (
          <div className="mt-2 text-sm text-yellow-300">
            {purchaseMessage}
          </div>
        )}
      </div>

      {/* Списки: Активные панели / Архивные панели */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Активные панели */}
        <div className="rounded-xl bg-gray-900 p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-lg font-semibold">Активные панели</h2>
            <div className="text-xs text-gray-400">
              Генерация одной панели: {isVip ? `${ENERGY_PER_DAY_VIP}` : `${ENERGY_PER_DAY_BASE}`} кВт/сутки
            </div>
          </div>
          {activePanels.length === 0 ? (
            <div className="text-gray-400 text-sm">
              У вас пока нет активных панелей.
            </div>
          ) : (
            <ul className="space-y-2">
              {activePanels.map((p, idx) => (
                <li
                  key={idx}
                  className="flex items-center justify-between rounded-lg bg-gray-800 px-3 py-2"
                >
                  <div>
                    <div className="text-sm">Панель от {p.start_date}</div>
                    <div className="text-xs text-gray-400">
                      Осталось дней: {p.days_left ?? "-"}
                    </div>
                  </div>
                  <div className="text-xs text-gray-500">
                    Активна до {getEndDateLabel(p.start_date, DAYS_ACTIVE)}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Архивные панели */}
        <div className="rounded-xl bg-gray-900 p-4">
          <h2 className="text-lg font-semibold mb-3">Архив панелей</h2>
          {archivedPanels.length === 0 ? (
            <div className="text-gray-400 text-sm">
              Архив пуст.
            </div>
          ) : (
            <ul className="space-y-2">
              {archivedPanels.map((p, idx) => (
                <li
                  key={idx}
                  className="flex items-center justify-between rounded-lg bg-gray-800 px-3 py-2"
                >
                  <div>
                    <div className="text-sm">Панель от {p.start_date}</div>
                    <div className="text-xs text-gray-400">Статус: завершена</div>
                  </div>
                  <div className="text-xs text-gray-500">
                    {/* Можем показать дату конца из start_date + DAYS_ACTIVE */}
                    Завершена {getEndDateLabel(p.start_date, DAYS_ACTIVE)}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      {/* Инфоблок */}
      <div className="mt-6 text-xs text-gray-400">
        <p>• Панель активна {DAYS_ACTIVE} дней. По завершении переносится в Архив.</p>
        <p>• С VIP (NFT в кошельке) генерация — {ENERGY_PER_DAY_VIP} кВт/сутки, без VIP — {ENERGY_PER_DAY_BASE} кВт/сутки.</p>
        <p>• Статус VIP рассчитывается ежедневно согласно наличию NFT в кошельке на бэкенде.</p>
        <p>• Покупка только за EFHC. Покупка EFHC за TON/USDT — через раздел Shop, пополнение EFHC — через TON-кошелёк с memo (ID).</p>
      </div>
    </div>
  );
}

/**
 * getEndDateLabel — вычисляет дату завершения панели: start_date + DAYS_ACTIVE
 * @param {string} startDateISO — дата начала в формате YYYY-MM-DD
 * @param {number} lifetimeDays — срок действия панели (обычно 180)
 * @returns {string} метка даты, например "2025-12-31"
 */
function getEndDateLabel(startDateISO, lifetimeDays) {
  try {
    const d = new Date(startDateISO);
    if (isNaN(d.getTime())) return "-";
    const endMs = d.getTime() + lifetimeDays * 24 * 60 * 60 * 1000;
    const end = new Date(endMs);
    const y = end.getFullYear();
    const m = String(end.getMonth() + 1).padStart(2, "0");
    const day = String(end.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  } catch {
    return "-";
  }
}

