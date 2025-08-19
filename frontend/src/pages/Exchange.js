import React, { useEffect, useMemo, useState } from "react";
import axios from "axios";

/**
 * Exchange.jsx — Страница "Обмен" (конвертация, переводы, вывод, привязка кошелька, ввод EFHC).
 * --------------------------------------------------------------------------------------------
 * Принципы:
 *  • EFHC и кВт — разные балансы. Конвертировать можно только kWh → EFHC (1:1), и КАТЕГОРИЧЕСКИ не наоборот.
 *  • Панели покупаются только за EFHC — логика выводится на странице "Panels" и в магазине "Shop".
 *  • Ввод EFHC с внешнего кошелька — доступен: отправка EFHC на админский TON-кошелёк с memo = Telegram ID.
 *  • Вывод EFHC — запрос в админку, обрабатывается вручную. Списывает EFHC с баланса пользователя, создаёт заявку.
 *  • Перевод EFHC по Telegram ID — внутренний перевод с логированием.
 *  • Привязка TON-кошелька к аккаунту — обязательна для вывода.
 *
 * Бэкенд:
 *  - GET /api/exchange/:user_id — получение текущих балансов и статуса:
 *      {
 *        "user_id": 123456789,
 *        "efhc_balance": 75.200,
 *        "kwh_generated_total": 10800.453,
 *        "kwh_available": 1260.000,
 *        "wallet_bound": "UQA...KW" | null,
 *        "nft_status": true | false,
 *        "ton_deposit_address": "UQAyCoxmxzb2D6cmlf..." // адрес для депозита EFHC
 *      }
 *
 *  - POST /api/exchange/wallet-bind:
 *      { "user_id": 123456789, "ton_address": "UQA..." }
 *    Ответ: { "success": true, "message": "Wallet bound" }
 *
 *  - POST /api/exchange/convert:
 *      { "user_id": 123456789, "amount_kwh": 100.0 }
 *    Ответ: { "success": true, "message": "Converted" }
 *
 *  - POST /api/exchange/transfer:
 *      { "from": 123456789, "to_telegram_id": 987654321, "amount": 5.0 }
 *    Ответ: { "success": true, "message": "Transferred" }
 *
 *  - POST /api/exchange/withdraw:
 *      { "user_id": 123456789, "amount": 50.0 }
 *    Ответ: { "success": true, "message": "Withdrawal requested" }
 *
 * Важно:
 *  • Все числовые поля должны форматироваться до 3 знаков после запятой (для кВт/EFHC, где нужно).
 *  • Кнопки отключаются, если не хватает баланса или не введены корректные данные.
 *  • Страница адаптивна (Tailwind CSS), с тёмной схемой.
 *
 * Взаимодействие с UI:
 *  • Есть подсказки и предупреждения для пользователя.
 *  • Сообщения об успехе/ошибках отрисовываются под соответствующими блоками.
 */

export default function Exchange({ userId }) {
  // --- Общие состояния (балансы и статусы)
  const [loading, setLoading] = useState(true);              // флаг загрузки данных
  const [error, setError] = useState(null);                  // текст ошибки
  const [efhcBalance, setEfhcBalance] = useState(0);         // баланс EFHC (внутренний)
  const [kwhTotal, setKwhTotal] = useState(0);               // общая сгенерированная энергия (kWh)
  const [kwhAvailable, setKwhAvailable] = useState(0);       // доступно для обмена (kWh)
  const [walletBound, setWalletBound] = useState("");        // привязанный TON-адрес пользователя (если есть)
  const [nftStatus, setNftStatus] = useState(false);         // есть ли NFT в кошельке
  const [tonDepositAddress, setTonDepositAddress] = useState(""); // адрес для депозита EFHC (админский кошелек)

  // --- Привязка кошелька: ввод адреса
  const [tonAddressInput, setTonAddressInput] = useState("");
  const [bindLoading, setBindLoading] = useState(false);
  const [bindMessage, setBindMessage] = useState("");

  // --- Конвертация kWh → EFHC
  const [convertAmount, setConvertAmount] = useState("");  // строковое поле ввода
  const [convertLoading, setConvertLoading] = useState(false);
  const [convertMessage, setConvertMessage] = useState("");

  // --- Перевод EFHC внутри приложения
  const [transferId, setTransferId] = useState("");            // Telegram ID получателя (строка)
  const [transferAmount, setTransferAmount] = useState("");    // сумма EFHC для перевода (строка)
  const [transferLoading, setTransferLoading] = useState(false);
  const [transferMessage, setTransferMessage] = useState("");

  // --- Вывод EFHC
  const [withdrawAmount, setWithdrawAmount] = useState("");     // сумма EFHC (строка)
  const [withdrawLoading, setWithdrawLoading] = useState(false);
  const [withdrawMessage, setWithdrawMessage] = useState("");

  /**
   * fetchExchangeData — загрузка данных по обмену и кошелькам.
   * GET /api/exchange/:userId
   */
  async function fetchExchangeData() {
    try {
      setLoading(true);
      setError(null);

      const url = `/api/exchange/${userId}`;
      const res = await axios.get(url);

      const data = res.data || {};
      setEfhcBalance(Number(data.efhc_balance || 0));
      setKwhTotal(Number(data.kwh_generated_total || 0));
      setKwhAvailable(Number(data.kwh_available || 0));
      setWalletBound(data.wallet_bound || "");
      setNftStatus(Boolean(data.nft_status));
      setTonDepositAddress(data.ton_deposit_address || "");

      setLoading(false);
    } catch (err) {
      console.error("Ошибка загрузки Exchange данных:", err);
      setError("Не удалось загрузить данные. Повторите попытку позже.");
      setLoading(false);
    }
  }

  // Первичная загрузка данных
  useEffect(() => {
    fetchExchangeData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  // --- Валидаторы и вычисления для кнопок
  const canConvert = useMemo(() => {
    const amt = Number(convertAmount);
    return !isNaN(amt) && amt > 0 && amt <= kwhAvailable;
  }, [convertAmount, kwhAvailable]);

  const canTransfer = useMemo(() => {
    const toId = Number(transferId);
    const amt = Number(transferAmount);
    return (
      !isNaN(toId) && toId > 0 &&
      !isNaN(amt) && amt > 0 && amt <= efhcBalance
    );
  }, [transferId, transferAmount, efhcBalance]);

  const canWithdraw = useMemo(() => {
    const amt = Number(withdrawAmount);
    return !isNaN(amt) && amt > 0 && amt <= efhcBalance && walletBound;
  }, [withdrawAmount, efhcBalance, walletBound]);

  const canBindWallet = useMemo(() => {
    const addr = (tonAddressInput || "").trim();
    // Валидация: минимальная длина и префикс (упрощённо). В реальном проекте можно использовать строгую проверку TON-адреса (Base64/URL).
    return addr.length > 10 && (addr.startsWith("UQ") || addr.startsWith("EQ"));
  }, [tonAddressInput]);

  /**
   * handleBindWallet — привязка TON-кошелька к аккаунту.
   * POST /api/exchange/wallet-bind { user_id, ton_address }
   */
  async function handleBindWallet() {
    setBindMessage("");
    if (!canBindWallet) {
      setBindMessage("Неверный формат адреса TON. Проверьте адрес и попробуйте вновь.");
      return;
    }
    try {
      setBindLoading(true);
      const body = {
        user_id: userId,
        ton_address: tonAddressInput.trim(),
      };
      const res = await axios.post("/api/exchange/wallet-bind", body);
      const ok = Boolean(res?.data?.success);
      const msg = res?.data?.message || (ok ? "Кошелёк успешно привязан." : "Не удалось привязать кошелёк.");

      setBindMessage(msg);
      setBindLoading(false);

      if (ok) {
        // Обновляем локальный стейт привязки
        setWalletBound(body.ton_address);
        setTonAddressInput("");
      }
    } catch (err) {
      console.error("Ошибка привязки кошелька:", err);
      setBindMessage("Ошибка при привязке кошелька. Попробуйте позже.");
      setBindLoading(false);
    }
  }

  /**
   * handleConvert — конвертация kWh → EFHC по курсу 1:1.
   * POST /api/exchange/convert { user_id, amount_kwh }
   */
  async function handleConvert() {
    setConvertMessage("");
    if (!canConvert) {
      setConvertMessage("Введите корректное количество кВт для обмена (не больше доступного).");
      return;
    }
    try {
      setConvertLoading(true);
      const amt = Number(convertAmount);
      const payload = {
        user_id: userId,
        amount_kwh: amt,
      };
      const res = await axios.post("/api/exchange/convert", payload);

      const ok = Boolean(res?.data?.success);
      const msg = res?.data?.message || (ok ? "Обмен выполнен." : "Ошибка обмена.");

      setConvertMessage(msg);
      setConvertLoading(false);

      if (ok) {
        setConvertAmount("");
        // Обновить балансы
        await fetchExchangeData();
      }
    } catch (err) {
      console.error("Ошибка обмена:", err);
      setConvertMessage("Ошибка на сервере при обмене. Попробуйте позже.");
      setConvertLoading(false);
    }
  }

  /**
   * handleTransfer — внутренний перевод EFHC по Telegram ID.
   * POST /api/exchange/transfer { from, to_telegram_id, amount }
   */
  async function handleTransfer() {
    setTransferMessage("");
    if (!canTransfer) {
      setTransferMessage("Проверьте ввод: Telegram ID и сумма EFHC должны быть корректны.");
      return;
    }
    try {
      setTransferLoading(true);
      const payload = {
        from: userId,
        to_telegram_id: Number(transferId),
        amount: Number(transferAmount),
      };
      const res = await axios.post("/api/exchange/transfer", payload);

      const ok = Boolean(res?.data?.success);
      const msg = res?.data?.message || (ok ? "Перевод выполнен." : "Не удалось выполнить перевод.");

      setTransferMessage(msg);
      setTransferLoading(false);

      if (ok) {
        setTransferId("");
        setTransferAmount("");
        // Обновить EFHC баланс
        await fetchExchangeData();
      }
    } catch (err) {
      console.error("Ошибка перевода EFHC:", err);
      setTransferMessage("Ошибка сервера при переводе. Повторите позже.");
      setTransferLoading(false);
    }
  }

  /**
   * handleWithdraw — запрос на вывод EFHC.
   * POST /api/exchange/withdraw { user_id, amount }
   * Создаёт заявку (pending) в админ-панели.
   */
  async function handleWithdraw() {
    setWithdrawMessage("");
    if (!canWithdraw) {
      setWithdrawMessage("Недостаточно EFHC или кошелёк не привязан.");
      return;
    }
    try {
      setWithdrawLoading(true);
      const payload = {
        user_id: userId,
        amount: Number(withdrawAmount),
      };
      const res = await axios.post("/api/exchange/withdraw", payload);

      const ok = Boolean(res?.data?.success);
      const msg = res?.data?.message || (ok ? "Заявка на вывод отправлена." : "Не удалось отправить заявку.");

      setWithdrawMessage(msg);
      setWithdrawLoading(false);

      if (ok) {
        setWithdrawAmount("");
        await fetchExchangeData();
      }
    } catch (err) {
      console.error("Ошибка запроса вывода EFHC:", err);
      setWithdrawMessage("Ошибка сервера при обработке заявки. Попробуйте позже.");
      setWithdrawLoading(false);
    }
  }

  // --- UI: состояние загрузки и ошибки
  if (loading) {
    return <div className="text-center text-white py-20">Загрузка данных...</div>;
  }
  if (error) {
    return <div className="text-center text-red-400 py-20">{error}</div>;
  }

  // --- Компоненты статусов
  const nftStatusLabel = nftStatus ? (
    <span className="inline-block bg-green-600 text-white text-xs px-2 py-1 rounded-md">NFT: ✅ VIP</span>
  ) : (
    <span className="inline-block bg-gray-600 text-white text-xs px-2 py-1 rounded-md">NFT: ❌ Нет</span>
  );

  // --- Вспомогательная функция копирования в буфер
  async function copyToClipboard(text) {
    try {
      await navigator.clipboard.writeText(text);
      alert("Скопировано!");
    } catch (e) {
      console.warn("Clipboard API Error:", e);
      // fallback — можно добавить поле input и выделять/копировать иным способом
    }
  }

  return (
    <div className="w-full min-h-screen bg-black text-white px-4 py-6">
      {/* Верхняя сводка балансов и статусов */}
      <div className="mb-4 p-4 rounded-xl bg-gray-900">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {/* Сгенерировано всего */}
          <div className="rounded-lg bg-gray-800 px-3 py-3">
            <div className="text-xs text-gray-400">Общая сгенерированная энергия</div>
            <div className="text-2xl font-semibold text-yellow-300">
              {Number(kwhTotal).toFixed(3)} кВт·ч
            </div>
            <div className="text-xs text-gray-400">* Только энергия от панелей, без реферальных бонусов</div>
          </div>
          {/* Доступный кВт для обмена */}
          <div className="rounded-lg bg-gray-800 px-3 py-3">
            <div className="text-xs text-gray-400">Доступные для обмена</div>
            <div className="text-2xl font-semibold">
              {Number(kwhAvailable).toFixed(3)} кВт·ч
            </div>
          </div>
          {/* EFHC баланс */}
          <div className="rounded-lg bg-gray-800 px-3 py-3">
            <div className="text-xs text-gray-400">Баланс EFHC</div>
            <div className="text-2xl font-semibold">
              {Number(efhcBalance).toFixed(3)} EFHC
            </div>
          </div>
        </div>

        {/* Строка статуса NFT и кошелька */}
        <div className="mt-3 flex flex-col md:flex-row items-start md:items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            {nftStatusLabel}
            {walletBound ? (
              <span className="inline-block bg-blue-700 text-white text-xs px-2 py-1 rounded-md">
                Wallet: {walletBound.slice(0, 5)}...{walletBound.slice(-4)}
              </span>
            ) : (
              <span className="inline-block bg-gray-700 text-white text-xs px-2 py-1 rounded-md">
                Wallet: не привязан
              </span>
            )}
          </div>
          {/* Адрес для депозита EFHC */}
          {tonDepositAddress ? (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-gray-400">Адрес для депозита EFHC: </span>
              <span className="text-xs bg-gray-800 px-2 py-1 rounded">{tonDepositAddress}</span>
              <button
                onClick={() => copyToClipboard(tonDepositAddress)}
                className="text-xs px-2 py-1 rounded bg-yellow-600 hover:bg-yellow-500"
              >
                Копировать
              </button>
            </div>
          ) : (
            <div className="text-xs text-gray-400">Адрес для депозита EFHC временно недоступен.</div>
          )}
        </div>
      </div>

      {/* Привязка TON-кошелька */}
      <div className="mb-4 rounded-xl bg-gray-900 p-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Привязка TON-кошелька</h2>
          <div className="text-xs text-gray-400">
            Поддерживается: Tonkeeper, MyTonWallet, Telegram Wallet
          </div>
        </div>
        <div className="mt-3 grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3">
          <input
            type="text"
            placeholder="Введите адрес TON-кошелька (например, UQ...)"
            className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 outline-none"
            value={tonAddressInput}
            onChange={(e) => setTonAddressInput(e.target.value)}
          />
          <button
            onClick={handleBindWallet}
            disabled={!canBindWallet || bindLoading}
            className={`px-4 py-2 rounded-lg font-semibold ${
              canBindWallet && !bindLoading ? "bg-blue-700 hover:bg-blue-600" : "bg-gray-700 cursor-not-allowed"
            }`}
          >
            {bindLoading ? "Привязка..." : "Привязать"}
          </button>
        </div>
        {bindMessage && <div className="mt-2 text-sm text-yellow-300">{bindMessage}</div>}
      </div>

      {/* Обмен kWh → EFHC */}
      <div className="mb-4 rounded-xl bg-gray-900 p-4">
        <h2 className="text-lg font-semibold mb-2">Обмен энергии (кВт → EFHC)</h2>
        <div className="text-xs text-gray-400 mb-2">
          Курс фиксирован: <b>1 EFHC = 1 кВт.ч.</b> Обратная конвертация невозможна.
        </div>
        <div className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3">
          <input
            type="number"
            min="0"
            step="0.001"
            placeholder="Введите количество кВт для обмена"
            className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 outline-none"
            value={convertAmount}
            onChange={(e) => setConvertAmount(e.target.value)}
          />
          <button
            onClick={handleConvert}
            disabled={!canConvert || convertLoading}
            className={`px-4 py-2 rounded-lg font-semibold ${
              canConvert && !convertLoading ? "bg-green-600 hover:bg-green-500" : "bg-gray-700 cursor-not-allowed"
            }`}
          >
            {convertLoading ? "Обмен..." : "Обменять"}
          </button>
        </div>
        <div className="text-xs text-gray-400 mt-2">
          Доступно: {kwhAvailable.toFixed(3)} кВт·ч
        </div>
        {convertMessage && <div className="mt-2 text-sm text-yellow-300">{convertMessage}</div>}
      </div>

      {/* Перевод EFHC по Telegram ID */}
      <div className="mb-4 rounded-xl bg-gray-900 p-4">
        <h2 className="text-lg font-semibold mb-2">Перевод EFHC по Telegram ID</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <input
            type="number"
            placeholder="Telegram ID получателя"
            className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 outline-none"
            value={transferId}
            onChange={(e) => setTransferId(e.target.value)}
          />
          <div className="grid grid-cols-[1fr_auto] gap-3">
            <input
              type="number"
              min="0"
              step="0.001"
              placeholder="Сумма EFHC"
              className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 outline-none"
              value={transferAmount}
              onChange={(e) => setTransferAmount(e.target.value)}
            />
            <button
              onClick={handleTransfer}
              disabled={!canTransfer || transferLoading}
              className={`px-4 py-2 rounded-lg font-semibold ${
                canTransfer && !transferLoading ? "bg-orange-600 hover:bg-orange-500" : "bg-gray-700 cursor-not-allowed"
              }`}
            >
              {transferLoading ? "Перевод..." : "Отправить"}
            </button>
          </div>
        </div>
        <div className="text-xs text-gray-400 mt-2">
          Доступно: {efhcBalance.toFixed(3)} EFHC
        </div>
        {transferMessage && <div className="mt-2 text-sm text-yellow-300">{transferMessage}</div>}
      </div>

      {/* Вывод EFHC на привязанный кошелек */}
      <div className="mb-4 rounded-xl bg-gray-900 p-4">
        <h2 className="text-lg font-semibold mb-2">Вывод EFHC</h2>
        {walletBound ? (
          <>
            <div className="text-xs text-gray-400 mb-2">
              Вывод возможен только на привязанный кошелёк: <span className="text-white">{walletBound}</span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3">
              <input
                type="number"
                min="0"
                step="0.001"
                placeholder="Сумма EFHC для вывода"
                className="w-full bg-gray-800 text-white rounded-lg px-3 py-2 outline-none"
                value={withdrawAmount}
                onChange={(e) => setWithdrawAmount(e.target.value)}
              />
              <button
                onClick={handleWithdraw}
                disabled={!canWithdraw || withdrawLoading}
                className={`px-4 py-2 rounded-lg font-semibold ${
                  canWithdraw && !withdrawLoading ? "bg-purple-600 hover:bg-purple-500" : "bg-gray-700 cursor-not-allowed"
                }`}
              >
                {withdrawLoading ? "Отправка заявки..." : "Запросить вывод"}
              </button>
            </div>
            <div className="text-xs text-gray-400 mt-2">
              На балансе: {efhcBalance.toFixed(3)} EFHC
            </div>
          </>
        ) : (
          <div className="text-sm text-yellow-300">
            Привяжите TON-кошелёк, чтобы запросить вывод EFHC.
          </div>
        )}
        {withdrawMessage && <div className="mt-2 text-sm text-yellow-300">{withdrawMessage}</div>}
      </div>

      {/* Инструкция по вводу EFHC с кошелька пользователя */}
      <div className="mb-4 rounded-xl bg-gray-900 p-4">
        <h2 className="text-lg font-semibold mb-2">Пополнение EFHC с кошелька</h2>
        <div className="text-sm text-gray-300 space-y-2">
          <p>
            Чтобы пополнить баланс EFHC, отправьте EFHC-токены на адрес:
          </p>
          {tonDepositAddress ? (
            <div className="flex items-center gap-2">
              <span className="bg-gray-800 px-2 py-1 rounded">{tonDepositAddress}</span>
              <button
                onClick={() => copyToClipboard(tonDepositAddress)}
                className="text-xs px-2 py-1 rounded bg-yellow-600 hover:bg-yellow-500"
              >
                Копировать
              </button>
            </div>
          ) : (
            <div className="text-xs text-gray-400">Адрес для депозита EFHC недоступен.</div>
          )}
          <p>
            В поле <b>Memo/Комментарий</b> укажите ваш <b>Telegram ID</b>:
            <span className="ml-2 bg-gray-800 px-2 py-1 rounded">{userId}</span>
            <button
              onClick={() => copyToClipboard(String(userId))}
              className="ml-2 text-xs px-2 py-1 rounded bg-yellow-600 hover:bg-yellow-500"
            >
              Копировать ID
            </button>
          </p>
          <p>
            После прихода транзакции бот автоматически зачислит EFHC на ваш баланс. Вы получите уведомление.
          </p>
          <p className="text-xs text-gray-400">
            Убедитесь, что отправляете именно <b>EFHC</b>-токены в сети TON.
          </p>
        </div>
      </div>

      {/* Инфоблок — правила и заметки */}
      <div className="text-xs text-gray-400 space-y-1">
        <p>• Обмен энергии в EFHC возможен только в направлении kWh → EFHC. Обратный обмен запрещён.</p>
        <p>• Вывод EFHC обрабатывается администратором. Статус заявки: pending → approved → completed.</p>
        <p>• Покупка EFHC за TON/USDT — в разделе Shop. Пополнение EFHC также возможно прямым переводом.</p>
      </div>
    </div>
  );
}
