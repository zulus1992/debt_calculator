-- Права доступа к данным для ролей Supabase (Data API / PostgREST).
-- Применять: Supabase → SQL Editor → New query → вставить целиком → Run.
--
-- Зачем: таблицы создаёт db/schema.sql, но права (GRANT) выдаёт сама платформа Supabase.
-- Если их отозвали, или проект создавался нестандартно (таблицы в схеме public создавал
-- не тот владелец, права снимали вручную), PostgREST отвечает
-- «permission denied for schema public» (Postgres 42501) — и бот не может ни читать,
-- ни писать, хотя ключ верный и принят шлюзом.
--
-- Бот работает secret-ключом (sb_secret_…), которому соответствует роль service_role:
-- основной блок ниже — как раз для неё. Публичные ключи (publishable/anon) боту не нужны,
-- поэтому anon и authenticated по умолчанию не трогаем (это не ослабляет защиту проекта).

grant usage on schema public to service_role;
grant all on all tables in schema public to service_role;
grant all on all sequences in schema public to service_role;
grant all on all functions in schema public to service_role;

-- Чтобы права появлялись и у будущих таблиц (их создаёт db/schema.sql при обновлениях)
alter default privileges in schema public grant all on tables to service_role;
alter default privileges in schema public grant all on sequences to service_role;
alter default privileges in schema public grant all on functions to service_role;

-- Если приложение ходит в базу с публичным ключом (publishable/anon) и у таблиц
-- настроены политики RLS — раскомментируйте блок ниже:
-- grant usage on schema public to anon, authenticated;
-- grant all on all tables in schema public to anon, authenticated;
-- grant all on all sequences in schema public to anon, authenticated;
-- alter default privileges in schema public grant all on tables to anon, authenticated;

-- Проверка (должно вернуть true):
select has_schema_privilege('service_role', 'public', 'USAGE') as service_role_can_use_public;
