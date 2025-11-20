-- Telegram multi-hub routing schema (MVP)
create table if not exists tg_bindings (
  id bigserial primary key,
  chat_id bigint not null,
  hub_id text not null,
  alias text not null,
  is_default boolean not null default false,
  created_at timestamptz not null default now(),
  last_used_at timestamptz
);
create unique index if not exists ux_tg_bindings_chat_alias on tg_bindings(chat_id, alias);
create unique index if not exists ux_tg_bindings_chat_hub on tg_bindings(chat_id, hub_id);

create table if not exists tg_sessions (
  chat_id bigint primary key,
  current_hub_id text,
  source text not null default 'manual',
  updated_at timestamptz not null default now()
);

create table if not exists tg_topics (
  chat_id bigint not null,
  topic_id bigint not null,
  hub_id text not null,
  primary key(chat_id, topic_id)
);

create table if not exists tg_messages (
  tg_msg_id bigint primary key,
  chat_id bigint not null,
  hub_id text,
  alias text,
  routed_via text not null,
  created_at timestamptz not null default now()
);

-- optional hub -> chat/bot link for legacy outbound routes
create table if not exists tg_links (
  hub_id text primary key,
  owner_id text not null,
  bot_id text not null,
  chat_id text not null,
  updated_at timestamptz not null default now()
);
