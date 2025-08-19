import React, { useEffect, useState } from "react";
import axios from "axios";
import { useNavigate } from "react-router-dom";

/**
 * Компонент ProgressBar
 * Визуализирует прогресс пользователя до следующего уровня
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

/**
 * Слой природы (горы, река, деревья, солнце и тд.)
 * Отображается в зависимости от уровня пользователя
 */
function NatureLayer({ level }) {
  return (
    <div className="absolute inset-0 w-full h-full pointer-events-none z-10">
      {/* Река (уровень >= 1) */}
      {level >= 1 && (
        <div className="absolute bottom-0 left-0 w-full h-24 bg-blue-600 opacity-60"></div>
      )}
      {/* Деревья (уровень >= 3) */}
      {level >= 3 && (
        <div className="absolute bottom-24 left-10 text-green-600 text-6xl">🌳</div>
      )}
      {level >= 5 && (
        <div className="absolute bottom-24 right-12 text-green-600 text-6xl">🌲</div>
      )}
      {/* Горы (уровень >= 6) */}
      {level >= 6 && (
        <div className="absolute bottom-32 left-1/3 text-gray-500 text-7xl">⛰️</div>
      )}
      {/* Животные (уровень >= 8) */}
      {level >= 8 && (
        <div className="absolute bottom-28 right-1/4 text-yellow-200 text-5xl">🦌</div>
      )}
      {/* Птицы и солнце (уровень >= 10) */}
      {level >= 10 && (
        <div className="absolute top-8 left-1/4 text-yellow-400 text-5xl">☀️</div>
      )}
      {level >= 11 && (
        <div className="absolute top-12 right-1/3 text-white text-3xl">🕊️</div>
      )}
    </div>
  );
}

/**
 * Слой с электростанциями (СЭС, ВЭС, ГЭС)
 */
function EnergyStationsLayer({ level }) {
  return (
    <div className="absolute inset-0 w-full h-full pointer-events-none z-20">
      {/* СЭС — появляются постепенно (уровень >= 2,4,6,8) */}
      {level >= 2 && (
        <div className="absolute bottom-12 left-5 text-yellow-400 text-5xl">🔆</div>
      )}
      {level >= 4 && (
        <div className="absolute bottom-12 left-20 text-yellow-400 text-5xl">🔆</div>
      )}
      {/* ВЭС — появляются с уровней 6–9 */}
      {level >= 6 && (
        <div className="absolute bottom-20 right-12 text-white text-6xl">🌀</div>
      )}
      {level >= 9 && (
        <div className="absolute bottom-20 right-24 text-white text-6xl">🌀</div>
      )}
      {/* ГЭС — появляются с уровней 10–12 */}
      {level >= 10 && (
        <div className="absolute bottom-0 left-1/2 text-blue-400 text-5xl">💧</div>
      )}
      {level >= 12 && (
        <div className="absolute bottom-0 right-1/2 text-blue-400 text-5xl">💧</div>
      )}
    </div>
  );
}

/**
 * Главная страница Energy с многоуровневым слоистым интерфейсом
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

  const { total_energy, level, level_name, next_level, panels_active, gen_per_day } = stats;

  return (
    <div className="relative w-full min-h-screen bg-black overflow-hidden">
      {/* ===== Слой 1: Фон ===== */}
      <div className="absolute inset-0 bg-gradient-to-b from-gray-900 to-black z-0"></div>

      {/* ===== Слой 2: Природа ===== */}
      <NatureLayer level={level} />

      {/* ===== Слой 3: Станции (СЭС, ВЭС, ГЭС) ===== */}
      <EnergyStationsLayer level={level} />

      {/* ===== Слой 4: UI-Каркас ===== */}
      <div className="relative z-50 px-4 py-6 text-white">
        {/* Общая энергия и уровень */}
        <div className="text-center mb-4">
          <h1 className="text-3xl font-bold text-yellow-400">
            ⚡ {total_energy.toFixed(3)} kWh
          </h1>
          <p className="text-sm mt-1">
            {level}/12 — <span className="text-green-400">{level_name}</span>
          </p>
        </div>

        {/* Прогрессбар */}
        <ProgressBar current={total_energy} target={next_level} />

        <p className="text-xs text-center text-gray-400 mb-6">
          Осталось {(next_level - total_energy).toFixed(3)} kWh до следующего уровня
        </p>

        {/* Индикаторы */}
        <div className="grid grid-cols-3 gap-2 text-center">
          <button
            onClick={() => navigate("/panels")}
            className="bg-gray-800 rounded-xl py-3 hover:bg-gray-700 transition"
          >
            <div className="text-xl font-bold">{panels_active}</div>
            <div className="text-xs text-gray-400">Панелей</div>
          </button>

          <button
            onClick={() => navigate("/exchange")}
            className="bg-gray-800 rounded-xl py-3 hover:bg-gray-700 transition"
          >
            <div className="text-xl font-bold">{gen_per_day.toFixed(3)}</div>
            <div className="text-xs text-gray-400">кВт/сутки</div>
          </button>

          <button
            onClick={() => navigate("/ranks")}
            className="bg-gray-800 rounded-xl py-3 hover:bg-gray-700 transition"
          >
            <div className="text-xl font-bold">
              {(next_level - total_energy).toFixed(0)}
            </div>
            <div className="text-xs text-gray-400">До уровня</div>
          </button>
        </div>
      </div>
    </div>
  );
}
