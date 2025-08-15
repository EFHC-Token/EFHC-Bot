from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    tg_id = Column(Integer, unique=True, index=True, nullable=False)
    username = Column(String, index=True)
    lang = Column(String, default="ru")
    efhc_balance = Column(Float, default=0.0)
    kw_total = Column(Float, default=0.0)
    is_active_user = Column(Boolean, default=False)
    wallet_address = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class AdminBalance(Base):
    __tablename__ = "admin_balance"
    id = Column(Integer, primary_key=True)
    efhc_bank = Column(Float, default=10000.0)
    updated_at = Column(DateTime, default=datetime.utcnow)
