// 📄 frontend/src/pages/Withdraw.jsx
// -----------------------------------------------------------------------------
// Что делает страница:
//   • Позволяет пользователю создать заявку на вывод EFHC на свой TON-адрес.
//   • Поля формы: Telegram ID (readonly), TON-адрес, сумма EFHC.
//   • Отправляет запрос POST /api/withdraw (с верифицированным initData).
//   • Показывает список заявок пользователя: pending/sent/failed.
//   • Обновляет баланс EFHC в топ-баре (если используется контекст).
//
// Требования/зависимости:
//   • TailwindCSS для стилизации.
//   • Telegram WebApp SDK (window.Telegram.WebApp.initData / initDataUnsafe) — для подписи.
//   • BACKEND_API_URL в env-конфиге.
//   • Компонент TopBar.jsx содержится отдельно (здесь не используется).
// -----------------------------------------------------------------------------

import React, { useEffect, useMemo, useState } from "react";
import { toast } from "react-hot-toast";

// Утилита получения базового URL бекенда.
// Можно импортировать из глобальной утилиты api.js, если настроено.
const BACKEND_API_URL = import.meta.env.VITE_BACKEND_API_URL || "http://localhost:8000/api";

/**
 * Возвращает initData из Telegram WebApp (строка).
 * Используется для подписи запроса (валидация на бекенде).
 */
function getInitData() {
  if (window?.Telegram?.WebApp?.initData) {
    return window.Telegram.WebApp.initData;
  }
  return "";
}

/**
 * Возвращает безопасную структуру пользователя из initDataUnsafe.
 * Оттуда берём telegram_id, username, и т.п.
 */
function getUserFromInitDataUnsafe() {
  const user = window?.Telegram?.WebApp?.initDataUnsafe?.user;
  if (!user) return null;
  return {
    id: user.id,
    username: user.username ? `@${user.username}` : `id${user.id}`,
  };
}

const Withdraw = () => {
  // Пользователь из initDataUnsafe
  const tgUser = useMemo(() => getUserFromInitDataUnsafe(), []);
  const [telegramId, setTelegramId] = useState<number | null>(tgUser?.id || null);
  const [wallet, setWallet] = useState<string>(""); // TON-адрес пользователя
  const [amount, setAmount] = useState<string>(""); // EFHC сумма
  const [submitting, setSubmitting] = useState<boolean>(false);

  const [withdraws, setWithdraws] = useState<any[]>([]); // список заявок (пагинация — по мере необходимости)
  const [loadingList, setLoadingList] = useState<boolean>(false);

  useEffect(() => {
    // Инициализация WebApp визуально
    try {
      if (window?.Telegram?.WebApp) {
        window.Telegram.WebApp.enableClosingConfirmation();
        window.Telegram.WebApp.expand();
      }
    } catch (e) {
      console.error("Telegram WebApp init error:", e);
    }
  }, []);

  useEffect(() => {
    // Подгружаем список заявок на вывод для пользователя
    async function fetchWithdraws() {
      if (!telegramId) return;
      setLoadingList(true);
      try {
        const resp = await fetch(`${BACKEND_API_URL}/withdraw?user_id=${telegramId}`, {
          method: "GET",
          headers: {
            "Content-Type": "application/json",
            "X-Telegram-Init-Data": getInitData(), // передаем initData для верификации на бекенде
          },
        });
        const data = await resp.json();
        if (!resp.ok) {
          throw new Error(data?.detail || "Failed to fetch withdraws");
        }
        setWithdraws(Array.isArray(data.items) ? data.items : []);
      } catch (err: any) {
        console.error(err);
        toast.error(err?.message || "Ошибка загрузки заявок");
      } finally {
        setLoadingList(false);
      }
    }
    fetchWithdraws();
  }, [telegramId]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!telegramId) {
      toast.error("Не удалось определить Telegram ID");
      return;
    }
    if (!wallet || !/^UQ|EQ|0\:|kQ|-\w/.test(wallet)) {
      // Базовая проверка TON адреса. Можно расширить.
      toast.error("Некорректный TON-адрес");
      return;
    }
    const amt = parseFloat(amount);
    if (!amt || amt <= 0) {
      toast.error("Введите корректную сумму EFHC (> 0)");
      return;
    }

    setSubmitting(true);
    try {
      const resp = await fetch(`${BACKEND_API_URL}/withdraw`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Telegram-Init-Data": getInitData(),
        },
        body: JSON.stringify({
          user_id: telegramId,
          ton_address: wallet,
          amount_efhc: amt,
        }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        throw new Error(data?.detail || "Не удалось создать заявку");
      }
      toast.success("Заявка создана. Ожидайте подтверждения администратором");
      setAmount("");
      // Добавим в список новую заявку
      if (data?.withdrawal) {
        setWithdraws((prev) => [data.withdrawal, ...prev]);
      }
    } catch (err: any) {
      console.error(err);
      toast.error(err?.message || "Ошибка создания заявки");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="w-full h-full bg-gray-900 text-gray-100 p-4">
      {/* Заголовок блока */}
      <h1 className="text-xl font-bold mb-4">💸 Вывод EFHC</h1>

      {/* Форма создания заявки */}
      <form
        onSubmit={handleSubmit}
        className="bg-gray-800 rounded-lg p-4 flex flex-col gap-4"
      >
        <div>
          <label className="block text-sm text-gray-400 mb-1">Telegram ID</label>
          <input
            type="text"
            value={telegramId || ""}
            readOnly
            className="w-full bg-gray-700 text-gray-200 p-2 rounded border border-gray-600"
          />
        </div>

        <div>
          <label className="block text-sm text-gray-400 mb-1">TON-адрес получателя</label>
          <input
            type="text"
            placeholder="Например: EQD... или UQ..."
            value={wallet}
            onChange={(e) => setWallet(e.target.value)}
            className="w-full bg-gray-700 text-gray-200 p-2 rounded border border-gray-600"
          />
        </div>

        <div>
          <label className="block text-sm text-gray-400 mb-1">Сумма EFHC для вывода</label>
          <input
            type="number"
            step="0.001"
            min="0.001"
            placeholder="Например: 25.500"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            className="w-full bg-gray-700 text-gray-200 p-2 rounded border border-gray-600"
          />
        </div>

        <p className="text-xs text-gray-400">
          ⚠️ Внимание: Вывод EFHC производится только на привязанный TON-кошелёк. Выплата осуществляется в сети TON (Jetton EFHC). Статус «pending» означает, что заявка ожидает подтверждения администратором.
        </p>

        <button
          type="submit"
          disabled={submitting}
          className="bg-green-600 hover:bg-green-500 transition text-white font-semibold py-2 px-4 rounded disabled:opacity-50"
        >
          {submitting ? "Отправка..." : "Создать заявку"}
        </button>
      </form>

      {/* Список заявок пользователя */}
      <div className="mt-6">
        <h2 className="text-lg font-semibold mb-2">🧾 Мои заявки</h2>
        {loadingList ? (
          <div className="text-gray-400">Загрузка...</div>
        ) : (
          <div className="bg-gray-800 rounded-lg p-2">
            {withdraws.length === 0 ? (
              <div className="text-gray-400 px-2 py-4">Заявок пока нет.</div>
            ) : (
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="text-gray-400">
                    <th className="text-left py-2 px-2">ID</th>
                    <th className="text-left py-2 px-2">Дата</th>
                    <th className="text-left py-2 px-2">TON-адрес</th>
                    <th className="text-left py-2 px-2">Сумма EFHC</th>
                    <th className="text-left py-2 px-2">Статус</th>
                    <th className="text-left py-2 px-2">Tx Hash</th>
                  </tr>
                </thead>
                <tbody>
                  {withdraws.map((w) => (
                    <tr key={w.id} className="border-t border-gray-700">
                      <td className="py-2 px-2">{w.id}</td>
                      <td className="py-2 px-2">{new Date(w.created_at).toLocaleString()}</td>
                      <td className="py-2 px-2 truncate">{w.ton_address}</td>
                      <td className="py-2 px-2">{Number(w.amount).toFixed(3)}</td>
                      <td className="py-2 px-2">
                        {w.status === "pending" && <span className="text-yellow-400">Ожидание</span>}
                        {w.status === "sent" && <span className="text-green-400">Отправлено</span>}
                        {w.status === "failed" && <span className="text-red-400">Ошибка</span>}
                      </td>
                      <td className="py-2 px-2">{w.tx_hash || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>
    </div>
  );
};

export default Withdraw;
