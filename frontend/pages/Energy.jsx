import React, { useEffect, useState } from "react";
import axios from "axios";
import { useNavigate } from "react-router-dom";

/**
 * Energy.jsx — Главная страница "Energy"
 * -----------------------------------------------------------------------------
 * Что делает:
 *  • Показывает общую сгенерированную энергию (kWh), уровень (1–12), прогресс до следующего уровня.
 *  • Отображает индикаторы: активные панели, генерация в сутки, сколько осталось до уровня.
 *  • Использует 4 слоя:
 *      1) Фон (тёмная тема + градиент),
 *      2) Природа (анимированные SVG: река, солнце, птицы, деревья, горы),
 *      3) Станции (анимированные SVG: СЭС, ВЭС, ГЭС),
 *      4) UI-каркас (цифры, прогрессбар, кнопки).
 *  • Адаптивность — за счёт абсолютных блоков и пропорционального позиционирования (%).
 *
 * Зависимости:
 *  • axios — запросы к backend API (/api/energy/:userId)
 *  • TailwindCSS — стилизация
 *  • react-router-dom — навигация (на Panels, Exchange, Ranks)
 *
 * Примечание:
 *  • Верхняя панель (TopBar) не включена в эту страницу. Она вынесена в отдельный компонент (TopBar.jsx).
 *  • Данные берутся с backend:
 *      GET /api/energy/:userId
 *      → { total_energy, level, level_name, next_level, panels_active, gen_per_day }
 */

// ------------------------ Вспомогательные UI-компоненты ------------------------

/**
 * Компонент ProgressBar — визуализация прогресса до следующего уровня.
 * @param {number} current — текущая энергия (kWh)
 * @param {number} target — цель (kWh) для следующего уровня
 */
function ProgressBar({ current, target }) {
  const percent = Math.min((current / target) * 100, 100);
  return (
    <div className="w-full bg-gray-800 rounded-full h-4 overflow-hidden my-3">
      <div
        className="bg-yellow-500 h-4 transition-all duration-700"
        style={{ width: `${percent}%` }}
      ></div>
    </div>
  );
}

// ------------------------- Слой 2: Природа (SVG-анимации) ----------------------

/**
 * NatureLayerSVG — Анимированная природа:
 * - Река (анимация течения),
 * - Солнце (пульс/лучи),
 * - Птицы (анимация полёта),
 * - Деревья, горы — появляются согласно уровню.
 *
 * Отображение по уровням:
 *  lvl >= 1  → река, базовый фон природы
 *  lvl >= 3  → деревья
 *  lvl >= 6  → горы
 *  lvl >= 10 → солнце
 *  lvl >= 11 → птицы
 *
 * Компонент занимает весь экран (absolute inset-0), pointer-events: none.
 */
function NatureLayerSVG({ level }) {
  return (
    <div className="absolute inset-0 pointer-events-none z-10">
      {/* Встроенные стили для анимации SVG */}
      <style>{`
        /* Водная волна (анимация path d / offset) — имитация течения */
        @keyframes river-flow {
          0%   { transform: translateX(0%); }
          100% { transform: translateX(-50%); }
        }
        /* Пульсирующее солнце */
        @keyframes sun-pulse {
          0%   { r: 38; }
          50%  { r: 42; }
          100% { r: 38; }
        }
        /* Лучи солнца — плавная смена непрозрачности */
        @keyframes ray-fade {
          0%   { opacity: 0.6; }
          50%  { opacity: 1; }
          100% { opacity: 0.6; }
        }
        /* Полёт птиц (перемещение по X и качание по Y) */
        @keyframes bird-fly {
          0%   { transform: translate(0, 0); }
          25%  { transform: translate(100px, -10px); }
          50%  { transform: translate(200px, 0); }
          75%  { transform: translate(300px, -10px); }
          100% { transform: translate(400px, 0); }
        }
        .river {
          animation: river-flow 10s linear infinite;
        }
        .sun {
          animation: sun-pulse 4s ease-in-out infinite;
        }
        .sun-rays {
          animation: ray-fade 2.5s infinite;
        }
        .bird {
          animation: bird-fly 14s linear infinite;
        }
      `}</style>

      {/* Контейнер для SVG — адаптивное покрытие всей сцены */}
      <svg
        width="100%"
        height="100%"
        viewBox="0 0 1440 900"
        preserveAspectRatio="xMidYMid slice"
      >
        {/* Земля/фон природы — придаёт мягкий оттенок */}
        <rect x="0" y="0" width="1440" height="900" fill="transparent" />

        {/* Река (уровень >= 1) — декоративная лента внизу */}
        {level >= 1 && (
          <g>
            {/* Подложка: береговая линия (полупрозрачная) */}
            <rect x="0" y="780" width="1440" height="120" fill="#1f456a" opacity="0.3" />
            {/* Бегущая волна: два повторяющихся path'а */}
            <g transform="translate(0, 820)">
              <path
                className="river"
                d="
                  M0 0
                  C 120 10, 240 -10, 360 0
                  C 480 10, 600 -10, 720 0
                  C 840 10, 960 -10, 1080 0
                  C 1200 10, 1320 -10, 1440 0
                "
                fill="none"
                stroke="#4ebbf4"
                strokeWidth="10"
                transform="translate(0, 0)"
              />
              <path
                className="river"
                d="
                  M0 0
                  C 120 10, 240 -10, 360 0
                  C 480 10, 600 -10, 720 0
                  C 840 10, 960 -10, 1080 0
                  C 1200 10, 1320 -10, 1440 0
                "
                fill="none"
                stroke="#7ed8ff"
                strokeWidth="6"
                transform="translate(0, 8)"
                opacity="0.7"
              />
            </g>
          </g>
        )}

        {/* Деревья (уровень >= 3) — простые силуэты деревьев */}
        {level >= 3 && (
          <g opacity="0.9">
            {/* Левый блок деревьев */}
            <g transform="translate(100, 650)">
              <rect x="0" y="20" width="10" height="40" fill="#2d5a27" />
              <circle cx="5" cy="20" r="20" fill="#52a34e" />
            </g>
            {/* Правый блок деревьев */}
            {level >= 5 && (
              <g transform="translate(1260, 650)">
                <rect x="0" y="20" width="10" height="40" fill="#2d5a27" />
                <circle cx="5" cy="20" r="22" fill="#4ca14b" />
              </g>
            )}
            {/* Центральная группа деревьев */}
            {level >= 7 && (
              <g transform="translate(700, 620)">
                <rect x="0" y="20" width="10" height="40" fill="#2d5a27" />
                <circle cx="5" cy="20" r="26" fill="#53ad50" />
              </g>
            )}
          </g>
        )}

        {/* Горы (уровень >= 6) — полупрозрачные полигоны */}
        {level >= 6 && (
          <g opacity="0.7">
            <polygon points="200,700 400,500 600,700" fill="#4a4a4a" />
            <polygon points="400,700 650,450 900,700" fill="#5a5a5a" />
            <polygon points="650,700 900,500 1100,700" fill="#666666" />
          </g>
        )}

        {/* Солнце (уровень >= 10) — пульсирующая окружность с лучами */}
        {level >= 10 && (
          <g transform="translate(220, 180)">
            <circle className="sun" cx="0" cy="0" r="40" fill="#FFD34D" />
            {/* Лучи */}
            {[...Array(12)].map((_, i) => (
              <rect
                key={i}
                className="sun-rays"
                x="-2"
                y="-70"
                width="4"
                height="20"
                fill="#FFD34D"
                transform={`rotate(${(360 / 12) * i})`}
              />
            ))}
          </g>
        )}

        {/* Птицы (уровень >= 11) — группа арок, перемещающаяся по небу */}
        {level >= 11 && (
          <g transform="translate(200, 120)" className="bird" opacity="0.8">
            <path
              d="M0 0 q20 -10 40 0"
              fill="none"
              stroke="#ffffff"
              strokeWidth="2"
            />
            <path
              d="M20 0 q20 -10 40 0"
              fill="none"
              stroke="#ffffff"
              strokeWidth="2"
            />
          </g>
        )}
      </svg>
    </div>
  );
}

// ------------------- Слой 3: Электростанции (SVG-анимации) --------------------

/**
 * EnergyStationsLayerSVG — анимированные СЭС, ВЭС, ГЭС:
 * - СЭС (пульс подсветки панелей),
 * - ВЭС (вращение пропеллеров),
 * - ГЭС (вода течёт под шлюзами).
 *
 * Отображение по уровням:
 *  lvl >= 2 → появляются первые СЭС
 *  lvl >= 4 → появление второй СЭС
 *  lvl >= 6 → первая ВЭС (вращение)
 *  lvl >= 9 → вторая ВЭС
 *  lvl >= 10 → первая ГЭС
 *  lvl >= 12 → вторая ГЭС
 *
 * Компонент занимает весь экран (absolute inset-0), pointer-events: none.
 */
function EnergyStationsLayerSVG({ level }) {
  return (
    <div className="absolute inset-0 pointer-events-none z-20">
      <style>{`
        /* Вращение ветроколеса */
        @keyframes rotor-spin {
          from { transform: rotate(0deg); }
          to   { transform: rotate(360deg); }
        }
        .rotor {
          transform-origin: center;
          animation: rotor-spin 4s linear infinite;
        }

        /* Пульс подсветки панелей (СЭС) */
        @keyframes solar-pulse {
          0%   { opacity: 0.6; }
          50%  { opacity: 1; }
          100% { opacity: 0.6; }
        }
        .solar-glow {
          animation: solar-pulse 2.2s ease-in-out infinite;
        }

        /* Вода у ГЭС — бегущие полоски */
        @keyframes water-stream {
          0%   { transform: translateX(0px); }
          100% { transform: translateX(-60px); }
        }
        .hydro-water {
          animation: water-stream 3.5s linear infinite;
        }
      `}</style>

      <svg
        width="100%"
        height="100%"
        viewBox="0 0 1440 900"
        preserveAspectRatio="xMidYMid slice"
      >
        {/* СЭС — solar panels */}
        {level >= 2 && (
          <g transform="translate(150, 720)">
            {/* Панель 1 */}
            <rect x="0" y="0" width="100" height="40" fill="#4f78d1" />
            <rect className="solar-glow" x="0" y="0" width="100" height="40" fill="#78a4ff" opacity="0.3" />
            {/* Стойка */}
            <rect x="48" y="-30" width="4" height="30" fill="#555" />
          </g>
        )}

        {level >= 4 && (
          <g transform="translate(300, 710)">
            {/* Панель 2 */}
            <rect x="0" y="0" width="120" height="45" fill="#4f78d1" />
            <rect className="solar-glow" x="0" y="0" width="120" height="45" fill="#78a4ff" opacity="0.3" />
            {/* Стойка */}
            <rect x="58" y="-35" width="4" height="35" fill="#555" />
          </g>
        )}

        {/* ВЭС — wind turbines */}
        {level >= 6 && (
          <g transform="translate(1150, 620)">
            {/* Башня */}
            <rect x="45" y="40" width="10" height="80" fill="#cfcfcf" />
            {/* Ротор */}
            <g className="rotor" transform="translate(50, 40)">
              <polygon points="0,-30 6,-10 -6,-10" fill="#e3e3e3" />
              <polygon points="30,0 10,6 10,-6" fill="#e3e3e3" />
              <polygon points="0,30 -6,10 6,10" fill="#e3e3e3" />
              <polygon points="-30,0 -10,-6 -10,6" fill="#e3e3e3" />
            </g>
          </g>
        )}
        {level >= 9 && (
          <g transform="translate(1020, 640)">
            <rect x="45" y="40" width="10" height="70" fill="#cfcfcf" />
            <g className="rotor" transform="translate(50, 40)">
              <polygon points="0,-26 5,-10 -5,-10" fill="#e3e3e3" />
              <polygon points="26,0 10,5 10,-5" fill="#e3e3e3" />
              <polygon points="0,26 -5,10 5,10" fill="#e3e3e3" />
              <polygon points="-26,0 -10,-5 -10,5" fill="#e3e3e3" />
            </g>
          </g>
        )}

        {/* ГЭС — hydro station */}
        {level >= 10 && (
          <g transform="translate(700, 750)">
            {/* Плотина */}
            <rect x="0" y="0" width="180" height="30" fill="#9aa3a7" />
            {/* Вода (бегущие полосы) */}
            <g className="hydro-water" transform="translate(0, 30)">
              <rect x="0" y="0" width="60" height="10" fill="#5cc3ff" />
              <rect x="70" y="0" width="60" height="10" fill="#5cc3ff" />
              <rect x="140" y="0" width="60" height="10" fill="#5cc3ff" />
            </g>
          </g>
        )}
        {level >= 12 && (
          <g transform="translate(880, 760)">
            <rect x="0" y="0" width="160" height="24" fill="#9aa3a7" />
            <g className="hydro-water" transform="translate(0, 24)">
              <rect x="0" y="0" width="50" height="8" fill="#5cc3ff" />
              <rect x="60" y="0" width="50" height="8" fill="#5cc3ff" />
              <rect x="120" y="0" width="50" height="8" fill="#5cc3ff" />
            </g>
          </g>
        )}
      </svg>
    </div>
  );
}

// -------------------------- Основной компонент Energy --------------------------

/**
 * Energy — главная страница с 4 слоями и UI.
 *
 * Пропсы:
 *  @param {number} userId — Telegram ID пользователя (используется для API)
 *
 * Backend API:
 *  GET /api/energy/:userId
 *  Ответ:
 *    {
 *      total_energy: number,      // общая сгенерированная энергия
 *      level: number,             // от 1 до 12
 *      level_name: string,        // название уровня
 *      next_level: number,        // целевой порог следующего уровня (kWh)
 *      panels_active: number,     // активных панелей
 *      gen_per_day: number        // суммарная генерация всех панелей (kWh/сутки)
 *    }
 */
export default function Energy({ userId }) {
  const [stats, setStats] = useState(null);
  const navigate = useNavigate();

  useEffect(() => {
    axios
      .get(`/api/energy/${userId}`)
      .then((res) => setStats(res.data))
      .catch((err) => console.error("Ошибка загрузки energy:", err));
  }, [userId]);

  if (!stats) {
    return (
      <div className="text-center text-white py-20">Загрузка данных...</div>
    );
  }

  const {
    total_energy,
    level,
    level_name,
    next_level,
    panels_active,
    gen_per_day,
  } = stats;

  return (
    <div className="relative w-full min-h-screen bg-black overflow-hidden">
      {/* ===== Слой 1: Фон ===== */}
      <div className="absolute inset-0 bg-gradient-to-b from-gray-900 to-black z-0"></div>

      {/* ===== Слой 2: Природа (SVG) ===== */}
      <NatureLayerSVG level={level} />

      {/* ===== Слой 3: Станции (SVG) ===== */}
      <EnergyStationsLayerSVG level={level} />

      {/* ===== Слой 4: UI-каркас ===== */}
      <div className="relative z-50 px-4 py-6 text-white">
        {/* Общая энергия и уровень */}
        <div className="text-center mb-4">
          <h1 className="text-3xl font-bold text-yellow-400">
            ⚡ {Number(total_energy).toFixed(3)} kWh
          </h1>
          <p className="text-sm mt-1">
            {level}/12 — <span className="text-green-400">{level_name}</span>
          </p>
        </div>

        {/* Прогрессбар */}
        <ProgressBar current={Number(total_energy)} target={Number(next_level)} />

        <p className="text-xs text-center text-gray-400 mb-6">
          Осталось {Math.max(Number(next_level) - Number(total_energy), 0).toFixed(3)} kWh до следующего уровня
        </p>

        {/* Индикаторы — три кнопки */}
        <div className="grid grid-cols-3 gap-2 text-center">
          {/* Переход на список панелей */}
          <button
            onClick={() => navigate("/panels")}
            className="bg-gray-800 rounded-xl py-3 hover:bg-gray-700 transition"
          >
            <div className="text-xl font-bold">{panels_active}</div>
            <div className="text-xs text-gray-400">Панелей</div>
          </button>

          {/* Переход на обменник */}
          <button
            onClick={() => navigate("/exchange")}
            className="bg-gray-800 rounded-xl py-3 hover:bg-gray-700 transition"
          >
            <div className="text-xl font-bold">{Number(gen_per_day).toFixed(3)}</div>
            <div className="text-xs text-gray-400">кВт/сутки</div>
          </button>

          {/* Переход на рейтинг */}
          <button
            onClick={() => navigate("/ranks")}
            className="bg-gray-800 rounded-xl py-3 hover:bg-gray-700 transition"
          >
            <div className="text-xl font-bold">
              {Math.max(Number(next_level) - Number(total_energy), 0).toFixed(0)}
            </div>
            <div className="text-xs text-gray-400">До уровня</div>
          </button>
        </div>
      </div>
    </div>
  );
}
