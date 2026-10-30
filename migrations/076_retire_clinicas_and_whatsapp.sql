-- Retirada autorizada dos módulos Clínicas e WhatsApp em 30/10/2026 (SPEC-08).
-- Preserva 100% dos dados essenciais: perfis, vitais, remédios, cuidadores e contas.
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '120s';

-- 1. Expurgo das tabelas de Editorial e Clínicas (migração 067)
DROP TABLE IF EXISTS public.editorial_media CASCADE;
DROP TABLE IF EXISTS public.editorial_events CASCADE;
DROP TABLE IF EXISTS public.editorial_articles CASCADE;
DROP TABLE IF EXISTS public.editorial_members CASCADE;

-- 2. Expurgo das tabelas de Triagem e Atendimento WhatsApp (migrações 004, 009, 051, 057, 068)
DROP TABLE IF EXISTS public.whatsapp_intake_sessions CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_consents CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_deletions CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_reads CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_devices CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_handoffs CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_commands CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_events CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_media CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_messages CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_inbox CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_conversations CASCADE;
DROP TABLE IF EXISTS public.whatsapp_intake_operators CASCADE;
DROP TABLE IF EXISTS public.whatsapp_conversations CASCADE;
DROP TABLE IF EXISTS public.whatsapp_messages CASCADE;
DROP TABLE IF EXISTS public.whatsapp_confirmations CASCADE;
DROP TABLE IF EXISTS public.whatsapp_channel_privacy CASCADE;
