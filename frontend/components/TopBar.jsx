import React, { useState, useEffect } from "react";
import { Button } from "@/components/ui/button"; // общий UI-компонент
import { Globe } from "lucide-react"; // иконка для селектора языка
import axios from "axios";

/**
 * Глобальный верхний бар, который отображается на всех страницах WebApp.
 * Содержит:
 * - Telegram ID пользователя
 * - Username
 * - Баланс EFHC
 * - Кнопку "+" для пополнения EFHC с криптокошелька
 * - Кнопку "Shop" для открытия магазина
 * - Селектор языков (8 языков: EN, RU, UA, ES, FR, DE, CN, TR)
 *
 * Источник данных: бекенд API /api/user/:id
 */
export default function TopBar({ userId, onOpenWallet, onOpenShop, onChangeLanguage }) {
  const [user, setUser] = useState(null);
  const [language, setLanguage] = useState("EN");

  // Загружаем данные пользователя при монтировании
  useEffect(() => {
    axios
      .get(`/api/user/${userId}`)
      .then((res) => {
        setUser(res.data);
      })
      .catch((err) => console.error("Ошибка загрузки данных пользователя:", err));
  }, [userId]);

  const handleLanguageChange = (e) => {
    const newLang = e.target.value;
    setLanguage(newLang);
    if (onChangeLanguage) onChangeLanguage(newLang);
  };

  if (!user) {
    return (
      <div className="w-full bg-black text-white text-center p-2">
        Загрузка профиля...
      </div>
    );
  }

  return (
    <div className="w-full bg-black text-white flex items-center justify-between px-4 py-2 text-xs md:text-sm">
      {/* Левая часть: ID + username */}
      <div className="flex flex-col">
        <span>ID: {user.telegram_id}</span>
        <span>{user.username ? `@${user.username}` : "Без ника"}</span>
      </div>

      {/* Центр: Баланс EFHC + кнопка "+" + кнопка Shop */}
      <div className="flex items-center gap-2">
        <span className="text-yellow-400 font-bold">{user.efhc_balance.toFixed(2)} EFHC</span>
        <Button
          onClick={onOpenWallet}
          className="rounded-full bg-orange-600 hover:bg-orange-700 text-white px-3 py-1 text-xs"
        >
          +
        </Button>
        <Button
          onClick={onOpenShop}
          className="rounded-xl bg-orange-500 hover:bg-orange-600 text-white px-4 py-1 text-xs"
        >
          Shop
        </Button>
      </div>

      {/* Правая часть: селектор языков */}
      <div className="flex items-center gap-1">
        <Globe size={16} />
        <select
          value={language}
          onChange={handleLanguageChange}
          className="bg-black text-white border border-gray-600 rounded px-2 py-1 text-xs"
        >
          <option value="EN">EN</option>
          <option value="RU">RU</option>
          <option value="UA">UA</option>
          <option value="ES">ES</option>
          <option value="FR">FR</option>
          <option value="DE">DE</option>
          <option value="CN">中文</option>
          <option value="TR">TR</option>
        </select>
      </div>
    </div>
  );
}
