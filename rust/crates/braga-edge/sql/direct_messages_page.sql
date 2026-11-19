-- CHAT-01: relay de sete dias; o histórico permanente pertence ao aparelho.
SELECT id::text AS message_id, program_id::text AS program_id, patient_user_id,
       sender_user_id, sender_role, body, created_at, read_at
  FROM program_direct_messages
 WHERE program_id = $1
   AND patient_user_id = $2
   AND created_at > NOW() - interval '7 days'
   AND ($3::timestamptz IS NULL OR created_at < $3
        OR ($4::uuid IS NOT NULL AND created_at = $3 AND id < $4))
 ORDER BY created_at DESC, id DESC
 LIMIT $5
