-- Recupera apenas vínculos inequívocos já emitidos pelo próprio endpoint.
-- Nome sem auditoria ou atribuído a pacientes diferentes permanece sem acesso.
INSERT INTO prescription_files(filename, patient_id)
SELECT filename, MIN(patient_id)
FROM (
  SELECT patient_id,
    substring(details->>'file_url' FROM '/api/files/prescriptions/([A-Za-z0-9_.-]+)$') AS filename
  FROM care_audit_trail
  WHERE action_type='PRESCRIPTION_ANALYZED'
) AS legacy
WHERE filename IS NOT NULL
GROUP BY filename
HAVING COUNT(DISTINCT patient_id)=1
ON CONFLICT (filename) DO NOTHING;
