SELECT
    EXISTS (SELECT 1 FROM profiles WHERE id = $1 AND account_mode IN ('BUSINESS_ONLY', 'DUAL')) AS business_account,
    EXISTS (SELECT 1 FROM partner_programs WHERE owner_user_id = $1 AND closed_at IS NULL) AS owner,
    EXISTS (
        SELECT 1 FROM program_enrollments e
        JOIN partner_programs p ON p.id = e.program_id
        WHERE e.patient_user_id = $1 AND e.status = 'ACTIVE'
          AND p.closed_at IS NULL AND p.approval_status = 'APPROVED'
    ) AS participant,
    EXISTS (
        SELECT 1 FROM family_bindings f
        JOIN program_enrollments e ON e.patient_user_id = f.patient_id
        JOIN partner_programs p ON p.id = e.program_id
        WHERE f.caregiver_id = $1 AND f.status = 'ACTIVE'
          AND e.status = 'ACTIVE' AND e.family_share_enabled
          AND p.closed_at IS NULL AND p.approval_status = 'APPROVED'
          -- Os handlers leem a config via serde (camelCase com alias snake_case);
          -- o default da migration 091 usa snake_case. Ler as duas chaves evita
          -- 403 em config legada. Prefere a chave camelCase escrita pelo Rust.
          AND COALESCE(
              p.experience_config->'features'->>'familyLinkEnabled',
              p.experience_config->'features'->>'family_link_enabled'
          ) = 'true'
    ) AS family
