import React, { useEffect, useState } from "react";
import axios from "axios";

/**
 * Shop.jsx — Страница "Магазин"
 * --------------------------------
 * Теперь магазин поддерживает:
 *  • Покупку EFHC за TON / USDT.
 *  • VIP NFT:
 *    - Внешняя покупка (GetGems).
 *    - Внутренняя покупка в магазине за EFHC / TON / USDT.
 *  • Привязанный TON-кошелёк.
 *  • Гибкая система товаров (backend отдаёт JSON массив товаров).
 *
 * Backend API:
 *  - GET  /api/shop/config
 *    {
 *      "ton_wallet": "EQxxx...",
 *      "usdt_wallet": "0x...",
 *      "nft_market_url": "https://getgems.io/collection/EFHC",
 *      "user_wallet": "EQyyy...",
 *      "memo": "UID-123456",
 *      "items": [
 *         { "id": "vip_nft", "title": "VIP NFT", "desc": "+7% генерации", "price_efhc": 250, "price_ton": 20, "price_usdt": 50 },
 *         { "id": "booster_1", "title": "Бустер ⚡", "desc": "+10% на 24ч", "price_efhc": 50 },
 *         { "id": "skin_tree", "title": "Декор 🌳", "desc": "Уникальное дерево на фоне", "price_efhc": 100 }
 *      ]
 *    }
 *
 *  - POST /api/shop/buy
 *    { "user_id": 123456, "item_id": "vip_nft", "method": "efhc" }
 *    -> backend проверяет баланс и проводит покупку.
 */

export default function Shop({ userId }) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Общие данные
  const [tonWallet, setTonWallet] = useState("");
  const [usdtWallet, setUsdtWallet] = useState("");
  const [nftMarketUrl, setNftMarketUrl] = useState("");
  const [userWallet, setUserWallet] = useState("");
  const [memo, setMemo] = useState("");

  // Товары
  const [items, setItems] = useState([]);

  // Статус покупки
  const [purchaseStatus, setPurchaseStatus] = useState(null); // { item, method, status }

  /** Загружаем конфиг магазина */
  async function loadConfig() {
    try {
      setLoading(true);
      setError(null);

      const res = await axios.get("/api/shop/config", { params: { user_id: userId } });
      const data = res.data || {};

      setTonWallet(data.ton_wallet || "");
      setUsdtWallet(data.usdt_wallet || "");
      setNftMarketUrl(data.nft_market_url || "");
      setUserWallet(data.user_wallet || "");
      setMemo(data.memo || "");
      setItems(data.items || []);

      setLoading(false);
    } catch (err) {
      console.error("Ошибка загрузки config:", err);
      setError("Не удалось загрузить данные магазина");
      setLoading(false);
    }
  }

  useEffect(() => {
    loadConfig();
  }, [userId]);

  /** Покупка товара через backend */
  async function handleBuy(itemId, method) {
    try {
      setPurchaseStatus({ item: itemId, method, status: "pending" });
      const res = await axios.post("/api/shop/buy", {
        user_id: userId,
        item_id: itemId,
        method: method, // "efhc" | "ton" | "usdt"
      });

      if (res.data && res.data.success) {
        setPurchaseStatus({ item: itemId, method, status: "success" });
      } else {
        setPurchaseStatus({ item: itemId, method, status: "error" });
      }
    } catch (err) {
      console.error("Ошибка покупки:", err);
      setPurchaseStatus({ item: itemId, method, status: "error" });
    }
  }

  return (
    <div className="w-full min-h-screen bg-black text-white px-4 py-6">
      <h1 className="text-xl font-semibold mb-4">Магазин EFHC</h1>

      {loading ? (
        <div className="text-gray-300">Загрузка...</div>
      ) : error ? (
        <div className="text-red-400">{error}</div>
      ) : (
        <div className="space-y-6">
          {/* Покупка EFHC за TON / USDT */}
          <div className="bg-gray-900 rounded-xl p-4 shadow-lg">
            <h2 className="text-lg font-semibold mb-2">Пополнить EFHC</h2>
            <p className="text-sm text-gray-400 mb-2">
              Отправьте TON или USDT на кошелёк проекта. Укажите <b>Memo</b> для идентификации.
            </p>
            <div className="bg-gray-800 rounded-lg p-2 mb-2">
              <div className="text-xs text-gray-400">TON-кошелёк</div>
              <div className="font-mono">{tonWallet}</div>
            </div>
            <div className="bg-gray-800 rounded-lg p-2 mb-2">
              <div className="text-xs text-gray-400">USDT-кошелёк</div>
              <div className="font-mono">{usdtWallet}</div>
            </div>
            <div className="bg-gray-800 rounded-lg p-2">
              <div className="text-xs text-gray-400">Ваш Memo</div>
              <div className="font-mono">{memo}</div>
            </div>
          </div>

          {/* Список товаров */}
          <div className="space-y-4">
            {items.map((item) => (
              <div key={item.id} className="bg-gray-900 rounded-xl p-4 shadow-lg">
                <h2 className="text-lg font-semibold">{item.title}</h2>
                <p className="text-sm text-gray-400">{item.desc}</p>

                <div className="mt-3 flex flex-wrap gap-2">
                  {item.price_efhc && (
                    <button
                      onClick={() => handleBuy(item.id, "efhc")}
                      className="px-3 py-2 rounded-lg bg-blue-700 hover:bg-blue-600 text-sm"
                    >
                      {item.price_efhc} EFHC
                    </button>
                  )}
                  {item.price_ton && (
                    <button
                      onClick={() => handleBuy(item.id, "ton")}
                      className="px-3 py-2 rounded-lg bg-green-700 hover:bg-green-600 text-sm"
                    >
                      {item.price_ton} TON
                    </button>
                  )}
                  {item.price_usdt && (
                    <button
                      onClick={() => handleBuy(item.id, "usdt")}
                      className="px-3 py-2 rounded-lg bg-purple-700 hover:bg-purple-600 text-sm"
                    >
                      {item.price_usdt} USDT
                    </button>
                  )}
                  {item.id === "vip_nft" && nftMarketUrl && (
                    <a
                      href={nftMarketUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="px-3 py-2 rounded-lg bg-yellow-700 hover:bg-yellow-600 text-sm"
                    >
                      Купить на GetGems
                    </a>
                  )}
                </div>

                {/* Статус покупки */}
                {purchaseStatus?.item === item.id && (
                  <div className="mt-2 text-sm">
                    {purchaseStatus.status === "pending" && (
                      <span className="text-yellow-400">⏳ Покупка обрабатывается...</span>
                    )}
                    {purchaseStatus.status === "success" && (
                      <span className="text-green-400">✅ Покупка успешна!</span>
                    )}
                    {purchaseStatus.status === "error" && (
                      <span className="text-red-400">❌ Ошибка при покупке.</span>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Ваш кошелёк */}
          <div className="bg-gray-900 rounded-xl p-4 shadow-lg">
            <h2 className="text-lg font-semibold mb-2">Ваш TON-кошелёк</h2>
            <div className="bg-gray-800 rounded-lg p-2 font-mono">{userWallet}</div>
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
