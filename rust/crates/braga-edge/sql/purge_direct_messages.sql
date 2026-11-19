-- CHAT-01: lotes pequenos, ordenados e sem esperar por linhas em uso.
DELETE FROM program_direct_messages
 WHERE id IN (
     SELECT id FROM program_direct_messages
      WHERE created_at <= NOW() - interval '7 days'
      ORDER BY created_at, id
      LIMIT 500
      FOR UPDATE SKIP LOCKED
 )
