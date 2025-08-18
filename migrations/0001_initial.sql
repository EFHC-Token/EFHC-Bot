-- 📂 migrations/0001_initial.sql — первичная схема БД EFHC
-- -----------------------------------------------------------------------------
-- Запустите этот файл один раз (через psql/Neon SQL editor) для создания схем и таблиц.
-- Дальше можно использовать миграции Alembic, но базовый слой уже готов.

CREATE SCHEMA IF NOT EXISTS efhc_core;
CREATE SCHEMA IF NOT EXISTS efhc_referrals;
CREATE SCHEMA IF NOT EXISTS efhc_admin;
CREATE SCHEMA IF NOT EXISTS efhc_lottery;
CREATE SCHEMA IF NOT EXISTS efhc_tasks;

SET search_path TO efhc_core, efhc_referrals, efhc_admin, efhc_lottery, efhc_tasks, public;

-- users
CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  telegram_id BIGINT UNIQUE,
  username VARCHAR(64),
  lang VARCHAR(8) DEFAULT 'RU',
  wallet_ton VARCHAR(128),
  main_balance NUMERIC(14,3) NOT NULL DEFAULT 0,
  bonus_balance NUMERIC(14,3) NOT NULL DEFAULT 0,
  total_generated_kwh NUMERIC(18,3) NOT NULL DEFAULT 0,
  todays_generated_kwh NUMERIC(18,3) NOT NULL DEFAULT 0,
  is_active_user BOOLEAN NOT NULL DEFAULT FALSE,
  has_vip BOOLEAN NOT NULL DEFAULT FALSE,
  referred_by BIGINT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_users_referred_by ON users(referred_by);

-- panels
CREATE TABLE IF NOT EXISTS panels (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
  purchase_date TIMESTAMPTZ DEFAULT now(),
  lifespan_days INT DEFAULT 180,
  daily_generation NUMERIC(10,3) DEFAULT 0.598,
  active BOOLEAN DEFAULT TRUE
);

-- transaction_logs
CREATE TABLE IF NOT EXISTS transaction_logs (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT,
  op_type VARCHAR(64) NOT NULL,
  amount NUMERIC(14,3) NOT NULL,
  source VARCHAR(32) NOT NULL,
  meta JSONB,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_tx_user_op ON transaction_logs(user_id, op_type);

-- referrals
CREATE TABLE IF NOT EXISTS referrals (
  id BIGSERIAL PRIMARY KEY,
  inviter_id BIGINT,
  invited_id BIGINT UNIQUE,
  is_active BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ref_inviter_active ON referrals(inviter_id, is_active);

CREATE TABLE IF NOT EXISTS referral_milestones (
  id BIGSERIAL PRIMARY KEY,
  inviter_id BIGINT,
  milestone INT,
  reward_efhc NUMERIC(14,3),
  awarded BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(inviter_id, milestone)
);

-- lotteries
CREATE TABLE IF NOT EXISTS lotteries (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(64) UNIQUE,
  title VARCHAR(128),
  prize_type VARCHAR(32),
  target_participants INT,
  ticket_price_efhc NUMERIC(14,3) DEFAULT 1,
  max_tickets_per_user INT DEFAULT 10,
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT now(),
  ended_at TIMESTAMPTZ,
  winner_user_id BIGINT
);

CREATE TABLE IF NOT EXISTS lottery_tickets (
  id BIGSERIAL PRIMARY KEY,
  lottery_id BIGINT REFERENCES lotteries(id) ON DELETE CASCADE,
  user_id BIGINT,
  ticket_number INT,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(lottery_id, user_id, ticket_number)
);

-- tasks
CREATE TABLE IF NOT EXISTS tasks (
  id BIGSERIAL PRIMARY KEY,
  type VARCHAR(32) NOT NULL,
  title VARCHAR(256) NOT NULL,
  url VARCHAR(512),
  available_count INT DEFAULT 0,
  reward_bonus_efhc NUMERIC(14,3) DEFAULT 1,
  is_active BOOLEAN DEFAULT FALSE,
  created_by_admin_id BIGINT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS task_completions (
  id BIGSERIAL PRIMARY KEY,
  task_id BIGINT REFERENCES tasks(id) ON DELETE CASCADE,
  user_id BIGINT,
  status VARCHAR(32) DEFAULT 'done',
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(task_id, user_id)
);

-- admin NFT access
CREATE TABLE IF NOT EXISTS admin_nft_whitelist (
  id BIGSERIAL PRIMARY KEY,
  nft_url VARCHAR(512) UNIQUE,
  enabled BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_nft_permissions (
  id BIGSERIAL PRIMARY KEY,
  admin_nft_id BIGINT REFERENCES admin_nft_whitelist(id) ON DELETE CASCADE,
  can_shop BOOLEAN DEFAULT FALSE,
  can_tasks BOOLEAN DEFAULT FALSE,
  can_lotteries BOOLEAN DEFAULT FALSE,
  can_users BOOLEAN DEFAULT FALSE,
  can_withdrawals BOOLEAN DEFAULT FALSE,
  can_panels BOOLEAN DEFAULT FALSE,
  can_all BOOLEAN DEFAULT FALSE
);

-- shop
CREATE TABLE IF NOT EXISTS shop_items (
  id BIGSERIAL PRIMARY KEY,
  code VARCHAR(64) UNIQUE,
  label VARCHAR(128),
  pay_asset VARCHAR(16),
  price NUMERIC(14,3),
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shop_orders (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT,
  item_code VARCHAR(64),
  pay_asset VARCHAR(16),
  status VARCHAR(32) DEFAULT 'pending',
  memo_telegram_id VARCHAR(64),
  tx_hash VARCHAR(256),
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- withdrawals + VIP requests
CREATE TABLE IF NOT EXISTS withdrawal_requests (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT,
  amount_efhc NUMERIC(14,3),
  status VARCHAR(32) DEFAULT 'pending',
  history JSONB,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vip_nft_requests (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT,
  pay_asset VARCHAR(16),
  status VARCHAR(32) DEFAULT 'pending',
  history JSONB,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

-- vip cache
CREATE TABLE IF NOT EXISTS vip_ownership_cache (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT UNIQUE,
  has_vip BOOLEAN DEFAULT FALSE,
  last_checked_at TIMESTAMPTZ DEFAULT now()
);
