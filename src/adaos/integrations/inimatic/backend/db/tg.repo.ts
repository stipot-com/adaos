import { Pool } from 'pg'

export type Binding = { chat_id: bigint, hub_id: string, alias: string, is_default: boolean }

let _pool: Pool | null = null

export function pg(): Pool {
  if (_pool) return _pool
  const url = process.env['PG_URL'] || ''
  _pool = new Pool({ connectionString: url })
  return _pool
}

let _schemaReady = false
export async function ensureSchema(): Promise<void> {
  if (_schemaReady) return
  const sql = `
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

-- nats ws token for hub auth
alter table if exists tg_bindings add column if not exists hub_nats_token text;

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

create table if not exists tg_links (
  hub_id text primary key,
  owner_id text not null,
  bot_id text not null,
  chat_id text not null,
  updated_at timestamptz not null default now()
);
`
  await pg().query(sql)
  _schemaReady = true
}

export async function listBindings(chatId: number): Promise<Binding[]> {
  const { rows } = await pg().query('select chat_id::bigint, hub_id, alias, is_default from tg_bindings where chat_id=$1 order by alias asc', [chatId])
  return rows
}

export async function getDefaultBinding(chatId: number): Promise<Binding | null> {
  const { rows } = await pg().query('select chat_id::bigint, hub_id, alias, is_default from tg_bindings where chat_id=$1 and is_default=true limit 1', [chatId])
  return rows[0] || null
}

export async function getByAlias(chatId: number, alias: string): Promise<Binding | null> {
  const { rows } = await pg().query('select chat_id::bigint, hub_id, alias, is_default from tg_bindings where chat_id=$1 and alias=$2 limit 1', [chatId, alias])
  return rows[0] || null
}

export async function upsertBinding(chatId: number, hubId: string, alias: string, isDefault = false): Promise<void> {
  await pg().query(
    `insert into tg_bindings(chat_id, hub_id, alias, is_default)
     values($1, $2, $3, $4)
     on conflict (chat_id, hub_id) do update set alias=excluded.alias,
       is_default = case when excluded.is_default then true else tg_bindings.is_default end,
       last_used_at = now()`,
    [chatId, hubId, alias, isDefault]
  )
  if (isDefault) await setDefault(chatId, alias)
}

export async function getHubToken(hubId: string): Promise<string | null> {
  const { rows } = await pg().query('select hub_nats_token from tg_bindings where hub_id=$1 and hub_nats_token is not null limit 1', [hubId])
  return (rows[0]?.hub_nats_token as string | undefined) || null
}

export async function setHubToken(hubId: string, token: string): Promise<void> {
  await pg().query('update tg_bindings set hub_nats_token=$2 where hub_id=$1', [hubId, token])
}

export async function ensureHubToken(hubId: string): Promise<string> {
  const existing = await getHubToken(hubId)
  if (existing) return existing
  const { randomBytes } = await import('crypto')
  const token = randomBytes(36).toString('base64url')
  await setHubToken(hubId, token)
  return token
}

export async function verifyHubToken(hubId: string, token: string): Promise<boolean> {
  const { rowCount } = await pg().query('select 1 from tg_bindings where hub_id=$1 and hub_nats_token=$2 limit 1', [hubId, token])
  return (rowCount || 0) > 0
}

export async function setDefault(chatId: number, alias: string): Promise<void> {
  await pg().query('update tg_bindings set is_default=false where chat_id=$1', [chatId])
  await pg().query('update tg_bindings set is_default=true where chat_id=$1 and alias=$2', [chatId, alias])
}

export async function renameAlias(chatId: number, alias: string, newAlias: string): Promise<boolean> {
  const { rowCount } = await pg().query('update tg_bindings set alias=$3 where chat_id=$1 and alias=$2', [chatId, alias, newAlias])
  return (rowCount || 0) > 0
}

export async function unlinkAlias(chatId: number, alias: string): Promise<boolean> {
  await pg().query('delete from tg_topics using tg_bindings where tg_topics.chat_id=tg_bindings.chat_id and tg_bindings.alias=$2 and tg_bindings.chat_id=$1 and tg_topics.hub_id=tg_bindings.hub_id', [chatId, alias])
  const { rowCount } = await pg().query('delete from tg_bindings where chat_id=$1 and alias=$2', [chatId, alias])
  return (rowCount || 0) > 0
}

export async function setSession(chatId: number, hubId: string, source: string): Promise<void> {
  await pg().query(
    `insert into tg_sessions(chat_id, current_hub_id, source)
     values($1, $2, $3)
     on conflict (chat_id) do update set current_hub_id=excluded.current_hub_id, source=excluded.source, updated_at=now()`,
    [chatId, hubId, source]
  )
}

export async function getSession(chatId: number): Promise<{ chat_id: number, current_hub_id: string | null } | null> {
  const { rows } = await pg().query('select chat_id::bigint as chat_id, current_hub_id from tg_sessions where chat_id=$1', [chatId])
  return rows[0] || null
}

export async function bindTopic(chatId: number, topicId: number, hubId: string): Promise<void> {
  await pg().query('insert into tg_topics(chat_id, topic_id, hub_id) values($1,$2,$3) on conflict (chat_id, topic_id) do update set hub_id=excluded.hub_id', [chatId, topicId, hubId])
}

export async function unbindTopic(chatId: number, topicId: number): Promise<void> {
  await pg().query('delete from tg_topics where chat_id=$1 and topic_id=$2', [chatId, topicId])
}

export async function getTopicHub(chatId: number, topicId: number): Promise<{ hub_id: string, alias: string } | null> {
  const { rows } = await pg().query(
    `select b.hub_id, b.alias from tg_topics t join tg_bindings b on b.chat_id=t.chat_id and b.hub_id=t.hub_id
     where t.chat_id=$1 and t.topic_id=$2 limit 1`, [chatId, topicId]
  )
  return rows[0] || null
}

export async function logMessage(chatId: number, tgMsgId: number, hubId: string | null, alias: string | null, via: string): Promise<void> {
  await pg().query('insert into tg_messages(tg_msg_id, chat_id, hub_id, alias, routed_via) values($1,$2,$3,$4,$5) on conflict (tg_msg_id) do nothing', [tgMsgId, chatId, hubId, alias, via])
}

export async function mapMsgToHub(tgMsgId: number): Promise<{ hub_id: string, alias: string } | null> {
  const { rows } = await pg().query('select hub_id, alias from tg_messages where tg_msg_id=$1', [tgMsgId])
  return rows[0] || null
}

// --- Hub↔Chat link (legacy outbound support) ---
export type TgLink = { hub_id: string, owner_id: string, bot_id: string, chat_id: string, updated_at: number }

export async function tgLinkSetDb(hub_id: string, owner_id: string, bot_id: string, chat_id: string): Promise<TgLink> {
  await pg().query(
    `insert into tg_links(hub_id, owner_id, bot_id, chat_id)
     values($1,$2,$3,$4)
     on conflict (hub_id) do update set owner_id=excluded.owner_id, bot_id=excluded.bot_id, chat_id=excluded.chat_id, updated_at=now()`,
    [hub_id, owner_id, bot_id, chat_id]
  )
  return { hub_id, owner_id, bot_id, chat_id, updated_at: Math.floor(Date.now()/1000) }
}

export async function tgLinkGetDb(hub_id: string): Promise<TgLink | null> {
  const { rows } = await pg().query('select hub_id, owner_id, bot_id, chat_id, extract(epoch from updated_at)::int as updated_at from tg_links where hub_id=$1', [hub_id])
  return rows[0] || null
}
