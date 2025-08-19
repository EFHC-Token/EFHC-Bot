import React, { useEffect, useMemo, useRef, useState } from "react";
import axios from "axios";

/**
 * Ranks.jsx — Страница "Рейтинг"
 * --------------------------------
 * Что отображается:
 *  • Две вкладки:
 *    1) Общий рейтинг (по добытой энергии с панелей) — без реферальных бонусов!
 *    2) Рейтинг по рефералам — активные рефералы + сумма полученных бонусов EFHC.
 *
 *  • В каждой вкладке:
 *    - Таблица Top 100 участников по соответствующему критерию.
 *    - Позиция текущего пользователя в рейтинге.
 *    - Подсветка (медали 🥇🥈🥉) для топ-3.
 *    - Кнопка "Я" — скролл к позиции текущего пользователя в топ-листе (если он в топ-100).
 *    - Подсветка строки текущего пользователя (рамка или фон).
 *
 * API:
 *  - GET /api/rating/energy
 *    {
 *      "user": {
 *        "position": 28,
 *        "username": "@eco_master",
 *        "energy_generated": 5476.765
 *      },
 *      "top": [
 *        {"position": 1, "username": "@green_king", "energy_generated": 20000.0},
 *        {"position": 2, "username": "@eco_light", "energy_generated": 19341.123},
 *        ...
 *      ]
 *    }
 *
 *  - GET /api/rating/referrals
 *    {
 *      "user": {
 *        "position": 9,
 *        "username": "@solar_guru",
 *        "referrals": 87,
 *        "bonus_received": 13.4
 *      },
 *      "top": [
 *        {"position": 1, "username": "@ref_master", "referrals": 812, "bonus_received": 125.6},
 *        ...
 *      ]
 *    }
 *
 * Примечания:
 *  • Общий рейтинг учитывает ТОЛЬКО генерируемую энергию от панелей (kWh) с точностью до 0.001.
 *  • Реферальный рейтинг учитывает количество активных рефералов и сумму бонусов EFHC.
 *  • Рейтинги обновляются раз в сутки (см. логику cron/scheduler в backend).
 *  • Компонент НЕ включает TopBar (он вынесен отдельно).
 *  • Тёмная тема (Tailwind), адаптивная сетка.
 */

// Тип вкладки
const TAB_ENERGY = "energy";
const TAB_REFERRALS = "referrals";

export default function Ranks({ userId }) {
  // --- Состояния вкладок
  const [activeTab, setActiveTab] = useState(TAB_ENERGY);    // текущая вкладка рейтинга

  // --- Состояния загрузки и ошибок
  const [loadingEnergy, setLoadingEnergy] = useState(true);
  const [errorEnergy, setErrorEnergy] = useState(null);
  const [loadingRefs, setLoadingRefs] = useState(false);
  const [errorRefs, setErrorRefs] = useState(null);

  // --- Данные по энергии
  const [energyUser, setEnergyUser] = useState({
    position: null,
    username: "",
    energy_generated: 0,
  });
  const [energyTop, setEnergyTop] = useState([]); // [{position, username, energy_generated}...]

  // --- Данные по рефералам
  const [refsUser, setRefsUser] = useState({
    position: null,
    username: "",
    referrals: 0,
    bonus_received: 0,
  });
  const [refsTop, setRefsTop] = useState([]); // [{position, username, referrals, bonus_received}...]

  // --- refs для "Я" — позиция текущего пользователя
  const energyListRef = useRef(null);
  const refsListRef = useRef(null);

  /**
   * fetchEnergyRating — загрузка общего рейтинга (по энергии).
   * GET /api/rating/energy
   */
  async function fetchEnergyRating() {
    try {
      setLoadingEnergy(true);
      setErrorEnergy(null);

      const url = "/api/rating/energy";
      const res = await axios.get(url); 
      const data = res.data || {};
      const user = data.user || {};
      const top = Array.isArray(data.top) ? data.top : [];

      setEnergyUser({
        position: user.position ?? null,
        username: user.username || "",
        energy_generated: Number(user.energy_generated || 0),
      });
      setEnergyTop(top.map(item => ({
        position: item.position,
        username: item.username || "",
        energy_generated: Number(item.energy_generated || 0),
      })));

      setLoadingEnergy(false);
    } catch (err) {
      console.error("Ошибка загрузки energy рейтинга:", err);
      setErrorEnergy("Не удалось загрузить общий рейтинг. Повторите позже.");
      setLoadingEnergy(false);
    }
  }

  /**
   * fetchReferralRating — загрузка рейтинга по рефералам.
   * GET /api/rating/referrals
   */
  async function fetchReferralRating() {
    try {
      setLoadingRefs(true);
      setErrorRefs(null);

      const url = "/api/rating/referrals";
      const res = await axios.get(url);
      const data = res.data || {};
      const user = data.user || {};
      const top = Array.isArray(data.top) ? data.top : [];

      setRefsUser({
        position: user.position ?? null,
        username: user.username || "",
        referrals: Number(user.referrals || 0),
        bonus_received: Number(user.bonus_received || 0),
      });
      setRefsTop(top.map(item => ({
        position: item.position,
        username: item.username || "",
        referrals: Number(item.referrals || 0),
        bonus_received: Number(item.bonus_received || 0),
      })));

      setLoadingRefs(false);
    } catch (err) {
      console.error("Ошибка загрузки referral рейтинга:", err);
      setErrorRefs("Не удалось загрузить рейтинг по рефералам. Повторите позже.");
      setLoadingRefs(false);
    }
  }

  /**
   * useEffect — первичная загрузка energy рейтинга по умолчанию,
   * а также загрузка реферального рейтинга при переключении.
   */
  useEffect(() => {
    // При первом рендере загружаем общий рейтинг
    fetchEnergyRating();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  useEffect(() => {
    // При переключении вкладок подгружаем данные при необходимости
    if (activeTab === TAB_REFERRALS && refsTop.length === 0 && !loadingRefs && !errorRefs) {
      fetchReferralRating();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab]);

  /**
   * handleScrollToMe — прокрутить к позиции текущего пользователя в списке топ-100 (если он в списке).
   * Сработает по активной вкладке.
   */
  function handleScrollToMe() {
    if (activeTab === TAB_ENERGY) {
      // Находим элемент списка, где username совпадает с energyUser.username и позиция в пределах массива
      const idx = energyTop.findIndex(item => (item.username || "").toLowerCase() === (energyUser.username || "").toLowerCase());
      if (idx !== -1 && energyListRef.current) {
        const container = energyListRef.current;
        const target = container.children[idx];
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      } else {
        alert("В топ-100 вы пока не вошли, но продолжайте генерировать энергию!");
      }
    } else {
      const idx = refsTop.findIndex(item => (item.username || "").toLowerCase() === (refsUser.username || "").toLowerCase());
      if (idx !== -1 && refsListRef.current) {
        const container = refsListRef.current;
        const target = container.children[idx];
        if (target) {
          target.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      } else {
        alert("В топ-100 вы пока не вошли, приглашайте активных рефералов!");
      }
    }
  }

  // Вычисленные подсказки для состояния пользователя в каждой вкладке
  const userEnergyLabel = useMemo(() => {
    const pos = energyUser.position;
    return pos ? `#${pos}` : "—";
  }, [energyUser.position]);

  const userRefsLabel = useMemo(() => {
    const pos = refsUser.position;
    return pos ? `#${pos}` : "—";
  }, [refsUser.position]);

  // Функция для отрисовки медалей для топ-3
  function renderMedal(position) {
    if (position === 1) return <span className="text-yellow-400 text-lg">🥇</span>;
    if (position === 2) return <span className="text-gray-300 text-lg">🥈</span>;
    if (position === 3) return <span className="text-yellow-700 text-lg">🥉</span>;
    return <span className="text-sm text-gray-300">#{position}</span>;
  }

  // Выделение строки текущего пользователя
  function isCurrentUserRow(username) {
    const uname = (username || "").toLowerCase();
    if (activeTab === TAB_ENERGY) {
      const me = (energyUser.username || "").toLowerCase();
      return uname === me && !!me;
    } else {
      const me = (refsUser.username || "").toLowerCase();
      return uname === me && !!me;
    }
  }

  return (
    <div className="w-full min-h-screen bg-black text-white px-4 py-6">
      {/* Заголовок с вкладками */}
      <div className="mb-4">
        <h1 className="text-xl font-semibold">Рейтинг</h1>
        <div className="mt-2 flex items-center gap-2">
          <button
            onClick={() => setActiveTab(TAB_ENERGY)}
            className={`px-4 py-2 rounded-t-md font-medium ${
              activeTab === TAB_ENERGY ? "bg-gray-800" : "bg-gray-700 hover:bg-gray-600"
            }`}
          >
            Общий рейтинг (энергия)
          </button>
          <button
            onClick={() => setActiveTab(TAB_REFERRALS)}
            className={`px-4 py-2 rounded-t-md font-medium ${
              activeTab === TAB_REFERRALS ? "bg-gray-800" : "bg-gray-700 hover:bg-gray-600"
            }`}
          >
            Рейтинг по рефералам
          </button>

          {/* Кнопка "Я" — скролл к своей позиции */}
          <button
            onClick={handleScrollToMe}
            className="ml-auto px-4 py-2 rounded-md bg-blue-700 hover:bg-blue-600 font-semibold"
            title="Прокрутить к моей позиции"
          >
            Я
          </button>
        </div>
      </div>

      {/* Краткая сводка пользователя в активной вкладке */}
      <div className="mb-4 rounded-lg bg-gray-900 p-4">
        {activeTab === TAB_ENERGY ? (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="rounded bg-gray-800 px-3 py-3">
              <div className="text-xs text-gray-400">Моя позиция</div>
              <div className="text-2xl font-semibold">
                {userEnergyLabel}
              </div>
            </div>
            <div className="rounded bg-gray-800 px-3 py-3">
              <div className="text-xs text-gray-400">Мой ник</div>
              <div className="text-2xl font-semibold">
                {energyUser.username || "—"}
              </div>
            </div>
            <div className="rounded bg-gray-800 px-3 py-3">
              <div className="text-xs text-gray-400">Сгенерировано кВт·ч (панели)</div>
              <div className="text-2xl font-semibold text-yellow-300">
                {Number(energyUser.energy_generated || 0).toFixed(3)}
              </div>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="rounded bg-gray-800 px-3 py-3">
              <div className="text-xs text-gray-400">Моя позиция</div>
              <div className="text-2xl font-semibold">
                {userRefsLabel}
              </div>
            </div>
            <div className="rounded bg-gray-800 px-3 py-3">
              <div className="text-xs text-gray-400">Мой ник</div>
              <div className="text-2xl font-semibold">
                {refsUser.username || "—"}
              </div>
            </div>
            <div className="rounded bg-gray-800 px-3 py-3">
              <div className="text-xs text-gray-400">Рефералы / Бонус EFHC</div>
              <div className="text-2xl font-semibold">
                {Number(refsUser.referrals || 0)} <span className="text-sm text-gray-300"> / </span>
                <span className="text-yellow-300">{Number(refsUser.bonus_received || 0).toFixed(3)}</span>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* Содержимое вкладок */}
      {activeTab === TAB_ENERGY ? (
        <div>
          {/* Описание правил по энергии */}
          <div className="text-xs text-gray-400 mb-2">
            Учитываются только кВт·ч, сгенерированные панелями (без реферальных бонусов).
            Обновление рейтинга — 1 раз в сутки.
          </div>

          {/* Список Top 100: энергия */}
          {loadingEnergy ? (
            <div className="text-center py-10 text-gray-300">Загрузка общего рейтинга...</div>
          ) : errorEnergy ? (
            <div className="text-center py-10 text-red-400">{errorEnergy}</div>
          ) : (
            <div className="rounded-lg bg-gray-900 p-2">
              {/* Заголовки таблицы */}
              <div className="px-3 py-2 grid grid-cols-[80px_1fr_200px] gap-2 text-xs text-gray-400 border-b border-gray-700">
                <div>Место</div>
                <div>Пользователь</div>
                <div className="text-right">Сгенерировано кВт·ч</div>
              </div>

              {/* Список элементов */}
              <div ref={energyListRef} className="max-h-[60vh] overflow-auto">
                {energyTop.map((item, idx) => {
                  const highlight = isCurrentUserRow(item.username);
                  return (
                    <div
                      key={`energy-${item.position}-${item.username}`}
                      className={`px-3 py-2 grid grid-cols-[80px_1fr_200px] gap-2 items-center
                        ${highlight ? "bg-gray-800 border border-blue-700 rounded-lg my-1" : "border-b border-gray-800"}`}
                    >
                      <div className="flex items-center gap-2">{renderMedal(item.position)}</div>
                      <div className="truncate">{item.username || "—"}</div>
                      <div className="text-right text-yellow-300">{Number(item.energy_generated).toFixed(3)}</div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      ) : (
        <div>
          {/* Описание по реферальному рейтингу */}
          <div className="text-xs text-gray-400 mb-2">
            В расчёт берутся только активные рефералы (купили хотя бы 1 панель).
            Показана также сумма EFHC, полученная за рефералов. Обновление — 1 раз в сутки.
          </div>

          {/* Список Top 100: рефералы */}
          {loadingRefs ? (
            <div className="text-center py-10 text-gray-300">Загрузка рейтинга по рефералам...</div>
          ) : errorRefs ? (
            <div className="text-center py-10 text-red-400">{errorRefs}</div>
          ) : (
            <div className="rounded-lg bg-gray-900 p-2">
              {/* Заголовки таблицы */}
              <div className="px-3 py-2 grid grid-cols-[80px_1fr_200px_180px] gap-2 text-xs text-gray-400 border-b border-gray-700">
                <div>Место</div>
                <div>Пользователь</div>
                <div className="text-right">Активные рефералы</div>
                <div className="text-right">Бонус EFHC</div>
              </div>

              {/* Список элементов */}
              <div ref={refsListRef} className="max-h-[60vh] overflow-auto">
                {refsTop.map((item, idx) => {
                  const highlight = isCurrentUserRow(item.username);
                  return (
                    <div
                      key={`refs-${item.position}-${item.username}`}
                      className={`px-3 py-2 grid grid-cols-[80px_1fr_200px_180px] gap-2 items-center
                        ${highlight ? "bg-gray-800 border border-blue-700 rounded-lg my-1" : "border-b border-gray-800"}`}
                    >
                      <div className="flex items-center gap-2">{renderMedal(item.position)}</div>
                      <div className="truncate">{item.username || "—"}</div>
                      <div className="text-right">{Number(item.referrals).toFixed(0)}</div>
                      <div className="text-right text-yellow-300">{Number(item.bonus_received).toFixed(3)}</div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Инфоблок о семантике рейтинга */}
      <div className="mt-4 text-xs text-gray-400 space-y-1">
        <p>• Общий рейтинг: отображает только добычу энергии панелями (kWh) с точностью до 0.001.</p>
        <p>• Рейтинг по рефералам: учитываются активные рефералы и суммарный полученный бонус EFHC.</p>
        <p>• Для получения бонуса реферал должен купить хотя бы одну панель.</p>
        <p>• Обновление лидеров — 1 раз в сутки.</p>
      </div>
    </div>
  );
}

