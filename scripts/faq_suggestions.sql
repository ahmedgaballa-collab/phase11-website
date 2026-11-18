-- صندوق الأسئلة في صفحة /faq
-- شغّل الملف ده مرة واحدة من Supabase: SQL Editor > New query > Run
create table if not exists public.faq_suggestions (
  id bigint generated always as identity primary key,
  question text not null check (char_length(question) between 5 and 500),
  page text,
  created_at timestamptz not null default now()
);

alter table public.faq_suggestions enable row level security;

-- الزوار يقدروا يبعتوا أسئلة بس، ومحدش يقدر يقراها غيرك من لوحة Supabase
drop policy if exists "anon can submit suggestions" on public.faq_suggestions;
create policy "anon can submit suggestions"
  on public.faq_suggestions for insert to anon
  with check (char_length(question) between 5 and 500);
