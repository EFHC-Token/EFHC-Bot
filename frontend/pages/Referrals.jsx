import React, { useState, useEffect } from "react";
import axios from "axios";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

/**
 * Страница "Рефералы"
 *
 * Содержит:
 * - Уникальную реферальную ссылку
 * - Прогресс и достижения
 * - Уровни достижений
 * - Список активных и неактивных рефералов
 *
 * API:
 * GET /api/referrals/:user_id
 */
export default function Referrals({ userId }) {
  const [data, setData] = useState(null);
  const [tab, setTab] = useState("active");

  useEffect(() => {
    axios
      .get(`/api/referrals/${userId}`)
      .then((res) => setData(res.data))
      .catch((err) => console.error("Ошибка загрузки рефералов:", err));
  }, [userId]);

  const copyLink = () => {
    navigator.clipboard.writeText(data.link);
    alert("Ссылка скопирована!");
  };

  if (!data) {
    return <div className="text-center p-4">Загрузка...</div>;
  }

  return (
    <div className="p-4 text-white">
      {/* Блок 1: реферальная ссылка */}
      <Card className="bg-gray-900 mb-4">
        <CardContent className="flex flex-col gap-2 p-4">
          <span className="text-sm">
            Приглашай друзей — получай бонусы EFHC. Только активные рефералы (купившие панель) приносят бонусы.
          </span>
          <input
            type="text"
            value={data.link}
            readOnly
            className="w-full bg-black border border-gray-600 rounded p-2 text-xs"
          />
          <div className="flex gap-2">
            <Button onClick={copyLink} className="bg-green-600 hover:bg-green-700 text-white">
              📋 Скопировать
            </Button>
            <Button className="bg-blue-600 hover:bg-blue-700 text-white">
              🔗 Поделиться
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Блок 2: прогресс и достижения */}
      <Card className="bg-gray-900 mb-4">
        <CardContent className="p-4">
          <div className="grid grid-cols-2 gap-2 text-sm">
            <div>Приглашено всего: {data.total}</div>
            <div>Активных: {data.active}</div>
            <div>Бонус начислено: {data.bonus} EFHC</div>
            <div>Уровень: {data.level} / {data.next_level}</div>
          </div>
          {/* Прогресс-бар */}
          <div className="w-full bg-gray-700 rounded-full h-3 mt-2">
            <div
              className="bg-green-500 h-3 rounded-full"
              style={{ width: `${(data.active / data.next_level) * 100}%` }}
            />
          </div>
          <p className="text-xs mt-1">
            Осталось {data.next_level - data.active} активных до следующего уровня
          </p>
        </CardContent>
      </Card>

      {/* Блок 3: список рефералов */}
      <div className="flex gap-2 mb-2">
        <Button
          className={`flex-1 ${tab === "active" ? "bg-green-600" : "bg-gray-700"}`}
          onClick={() => setTab("active")}
        >
          Активные
        </Button>
        <Button
          className={`flex-1 ${tab === "inactive" ? "bg-red-600" : "bg-gray-700"}`}
          onClick={() => setTab("inactive")}
        >
          Неактивные
        </Button>
      </div>
      <Card className="bg-gray-900">
        <CardContent className="p-4 text-sm">
          {data.referrals
            .filter((r) => (tab === "active" ? r.purchased : !r.purchased))
            .map((r, idx) => (
              <div key={idx} className="flex justify-between border-b border-gray-700 py-1">
                <span>{r.id}</span>
                <span>{r.purchased ? "✅ Куплена" : "❌ Нет"}</span>
              </div>
            ))}
        </CardContent>
      </Card>

      {/* Инфоблок */}
      <p className="text-xs text-gray-400 mt-4">
        Бонусы начисляются только за рефералов, купивших хотя бы одну панель.
        Статистика обновляется раз в сутки.
      </p>
    </div>
  );
}
