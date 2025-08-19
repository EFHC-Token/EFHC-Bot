import React, { useEffect, useState } from "react";
import axios from "axios";

/**
 * Shop.jsx — Страница "Магазин"
 * --------------------------------
 * Функции:
 *  • Покупка EFHC за TON или USDT:
 *    - Отображается TON-кошелёк проекта и Memo (comment), уникальный для пользователя.
 *    - Пользователь отправляет TON/USDT на указанный адрес с правильным Memo.
 *    - После подтверждения транзакции (обработчик ton_integration.py на backend),
 *      EFHC зачисляются на баланс пользователя.
 *
 *  • Покупка VIP NFT:
 *    - Кнопка для открытия NFT-маркета EFHC (коллекция EFHC).
 *    - При покупке NFT бот ежедневно проверяет кошелёк пользователя (backend cron).
 *    - Если NFT есть — даёт статус VIP (+7% к генерации энергии).
 *
 *  • Открытие привязанного TON-кошелька:
 *    - Отображается TON-адрес, привязанный к аккаунту.
 *    - Кнопка "Открыть кошелёк" ведёт на ton:// или tonkeeper://.
 *
 * API backend:
 *  - GET  /api/shop/config
 *    {
 *      "ton_wallet": "EQxxxxxxxx",
 *      "usdt_wallet": "0xABCD...",       // можно через TON Jetton или EVM
 *      "nft_market_url": "https://getgems.io/collection/EFHC",
 *      "user_wallet": "EQyyyyyyyy",      // привязанный адрес пользователя
 *      "memo": "UID-123456"              // уникальный Memo для переводов
 *    }
 *
 *  - POST /api/shop/check-payment
 *    { "memo": "UID-123456", "tx_hash": "..."}
 *    -> backend проверяет в ton_events_log, начисляет EFHC.
 *
 * Визуал:
 *  • Тёмная тема, TailwindCSS.
 *  • Карточки товаров: EFHC, VIP NFT.
 *  • Подробные инструкции для пользователя.
 */

export default function Shop({ userId }) {
  // --- Состояния
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Данные из backend /shop/config
  const [tonWallet, setTonWallet] = useState("");
  const [usdtWallet, setUsdtWallet] = useState("");
  const [nftMarketUrl, setNftMarketUrl] = useState("");
  const [userWallet, setUserWallet] = useState("");
  const [memo, setMemo] = useState("");

  // Статус покупки EFHC
  const [paymentStatus, setPaymentStatus] = useState(null); // null | "pending" | "success" | "error"

  /**
   * loadConfig — загрузка конфигурации магазина
   * GET /api/shop/config
   * Загружает адреса кошельков, ссылку на NFT маркет и Memo для переводов.
   */
  async function loadConfig() {
    try {
      setLoading(true);
      setError(null);

      const url = "/api/shop/config";
      const res = await axios.get(url, { params: { user_id: userId } });
      const data = res.data || {};

      setTonWallet(data.ton_wallet || "");
      setUsdtWallet(data.usdt_wallet || "");
      setNftMarketUrl(data.nft_market_url || "");
      setUserWallet(data.user_wallet || "");
      setMemo(data.memo || "");

      setLoading(false);
    } catch (err) {
      console.error("Ошибка загрузки shop config:", err);
      setError("Не удалось загрузить данные магазина. Повторите позже.");
      setLoading(false);
    }
  }

  useEffect(() => {
    loadConfig();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  /**
   * handleCheckPayment — ручная проверка платежа пользователем
   * POST /api/shop/check-payment
   */
  async function handleCheckPayment(txHash) {
    try {
      setPaymentStatus("pending");

      const url = "/api/shop/check-payment";
      const res = await axios.post(url, { memo, tx_hash: txHash });

      if (res.data && res.data.success) {
        setPaymentStatus("success");
      } else {
        setPaymentStatus("error");
      }
    } catch (err) {
      console.error("Ошибка проверки платежа:", err);
      setPaymentStatus("error");
    }
  }

  return (
    <div className="w-full min-h-screen bg-black text-white px-4 py-6">
      <h1 className="text-xl font-semibold mb-4">Магазин EFHC</h1>

      {loading ? (
        <div className="text-gray-300">Загрузка конфигурации...</div>
      ) : error ? (
        <div className="text-red-400">{error}</div>
      ) : (
        <div className="space-y-6">
          {/* Карточка 1: Покупка EFHC */}
          <div className="bg-gray-900 rounded-xl p-4 shadow-lg">
            <h2 className="text-lg font-semibold mb-2">Покупка EFHC за TON / USDT</h2>
            <p className="text-sm text-gray-400 mb-2">
              Отправьте TON или USDT на кошелёк проекта. Обязательно укажите <b>Memo</b> для вашей идентификации.
              1 EFHC = 1 kWh.
            </p>
            <div className="space-y-2">
              <div className="bg-gray-800 rounded-lg p-2">
                <div className="text-xs text-gray-400">TON-кошелёк проекта</div>
                <div className="font-mono break-all">{tonWallet}</div>
              </div>
              <div className="bg-gray-800 rounded-lg p-2">
                <div className="text-xs text-gray-400">USDT-кошелёк проекта</div>
                <div className="font-mono break-all">{usdtWallet}</div>
              </div>
              <div className="bg-gray-800 rounded-lg p-2">
                <div className="text-xs text-gray-400">Ваш уникальный Memo</div>
                <div className="font-mono">{memo}</div>
              </div>
            </div>

            <div className="mt-3">
              <button
                onClick={() => handleCheckPayment(prompt("Введите хэш вашей транзакции"))}
                className="px-4 py-2 rounded-lg bg-blue-700 hover:bg-blue-600"
              >
                Проверить мой платеж
              </button>
            </div>

            {paymentStatus === "pending" && (
              <p className="mt-2 text-yellow-400">⏳ Проверка транзакции...</p>
            )}
            {paymentStatus === "success" && (
              <p className="mt-2 text-green-400">✅ Успешно! EFHC зачислены на ваш баланс.</p>
            )}
            {paymentStatus === "error" && (
              <p className="mt-2 text-red-400">❌ Ошибка. Проверьте хэш транзакции и повторите.</p>
            )}
          </div>

          {/* Карточка 2: VIP NFT */}
          <div className="bg-gray-900 rounded-xl p-4 shadow-lg">
            <h2 className="text-lg font-semibold mb-2">VIP NFT (+7% к генерации)</h2>
            <p className="text-sm text-gray-400 mb-2">
              Купите VIP NFT из коллекции EFHC, чтобы активировать бонус 1.07x (+7%) к ежедневной генерации энергии.
              Статус VIP проверяется ежедневно в вашем TON-кошельке.
            </p>
            <a
              href={nftMarketUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-block px-4 py-2 rounded-lg bg-purple-700 hover:bg-purple-600 font-semibold"
            >
              Купить VIP NFT
            </a>
          </div>

          {/* Карточка 3: Привязанный кошелёк */}
          <div className="bg-gray-900 rounded-xl p-4 shadow-lg">
            <h2 className="text-lg font-semibold mb-2">Ваш TON-кошелёк</h2>
            <div className="bg-gray-800 rounded-lg p-2 font-mono break-all">{userWallet}</div>
            <a
              href={`ton://transfer/${userWallet}`}
              className="mt-3 inline-block px-4 py-2 rounded-lg bg-green-700 hover:bg-green-600 font-semibold"
            >
              Открыть кошелёк
            </a>
          </div>
        </div>
      )}
    </div>
  );
}

