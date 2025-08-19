import React, { useState, useEffect } from "react";
import { BrowserRouter as Router, Routes, Route, useNavigate } from "react-router-dom";
import axios from "axios";
import TopBar from "./components/TopBar";

// Страницы
import Energy from "./pages/Energy";
import Referrals from "./pages/Referrals";
import Exchange from "./pages/Exchange";
import Panels from "./pages/Panels";
import Ranks from "./pages/Ranks";
import Shop from "./pages/Shop";

/**
 * Нижняя панель навигации
 * 5 кнопок: Energy, Referral, Exchange, Panels, Ranks
 * + отдельная кнопка для админов (Admin Panel)
 */
function BottomNav({ isAdmin }) {
  const navigate = useNavigate();
  const [active, setActive] = useState("energy");

  const buttons = [
    { id: "energy", label: "⚡ Energy", route: "/" },
    { id: "referrals", label: "🤝 Referrals", route: "/referrals" },
    { id: "exchange", label: "🔁 Exchange", route: "/exchange" },
    { id: "panels", label: "🌞 Panels", route: "/panels" },
    { id: "ranks", label: "🏆 Ranks", route: "/ranks" },
  ];

  return (
    <div className="fixed bottom-0 w-full bg-black text-white flex justify-around py-2 border-t border-gray-800">
      {buttons.map((btn) => (
        <button
          key={btn.id}
          onClick={() => {
            setActive(btn.id);
            navigate(btn.route);
          }}
          className={`flex-1 mx-1 rounded-xl py-2 text-xs md:text-sm ${
            active === btn.id ? "bg-orange-700" : "bg-gray-800"
          }`}
        >
          {btn.label}
        </button>
      ))}
      {isAdmin && (
        <button
          onClick={() => navigate("/admin")}
          className="ml-2 bg-red-600 hover:bg-red-700 text-white rounded-xl px-4 py-2 text-xs md:text-sm"
        >
          ⚙️ Admin
        </button>
      )}
    </div>
  );
}

/**
 * Главный контейнер App
 * Подгружает глобальные данные пользователя
 * Передаёт их в TopBar и страницы
 */
export default function App() {
  const [user, setUser] = useState(null);
  const [language, setLanguage] = useState("EN");

  // ⚡ Получаем userId из Telegram WebApp initData (или тестово фиксируем)
  const userId = window.Telegram?.WebApp?.initDataUnsafe?.user?.id || 123456789;

  useEffect(() => {
    axios
      .get(`/api/user/${userId}`)
      .then((res) => setUser(res.data))
      .catch((err) => console.error("Ошибка загрузки пользователя:", err));
  }, [userId]);

  if (!user) {
    return <div className="bg-black text-white text-center h-screen flex items-center justify-center">Загрузка...</div>;
  }

  return (
    <Router>
      {/* Глобальная верхняя панель */}
      <TopBar
        userId={userId}
        onOpenWallet={() => alert("Открыть TON-кошелёк для пополнения EFHC")}
        onOpenShop={() => (window.location.href = "/shop")}
        onChangeLanguage={(lang) => setLanguage(lang)}
      />

      {/* Основной контент */}
      <div className="pt-2 pb-16 bg-black text-white min-h-screen">
        <Routes>
          <Route path="/" element={<Energy userId={userId} language={language} />} />
          <Route path="/referrals" element={<Referrals userId={userId} language={language} />} />
          <Route path="/exchange" element={<Exchange userId={userId} language={language} />} />
          <Route path="/panels" element={<Panels userId={userId} language={language} />} />
          <Route path="/ranks" element={<Ranks userId={userId} language={language} />} />
          <Route path="/shop" element={<Shop userId={userId} language={language} />} />
          {/* TODO: Admin Panel */}
        </Routes>
      </div>

      {/* Нижняя панель */}
      <BottomNav isAdmin={user.is_admin} />
    </Router>
  );
}
