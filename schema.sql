-- StrategioAI CRM — kjør én gang i Supabase SQL Editor

create table if not exists contacts (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  company text,
  email text,
  phone text,
  source text default 'manuell',  -- gmaps, nettside, ref, manuell
  status text default 'ny',       -- ny, kontaktet, møte, tilbud, kunde, tapt
  notes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists deals (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid references contacts(id) on delete cascade,
  title text not null,
  value numeric default 0,
  package text,   -- pakke1, pakke2, pakke3, engang
  status text default 'ny',  -- ny, kontaktet, møte, tilbud, vunnet, tapt
  notes text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create table if not exists activities (
  id uuid primary key default gen_random_uuid(),
  contact_id uuid references contacts(id) on delete cascade,
  deal_id uuid references deals(id) on delete set null,
  type text not null,   -- call, email, møte, notat
  note text,
  created_at timestamptz default now()
);

-- Oppdater updated_at automatisk
create or replace function update_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger contacts_updated_at before update on contacts
  for each row execute function update_updated_at();

create trigger deals_updated_at before update on deals
  for each row execute function update_updated_at();
