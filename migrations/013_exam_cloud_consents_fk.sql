-- 013_exam_cloud_consents_fk.sql
-- AUD-G20 (auditoria de codigo 20/09/2026): exam_cloud_consents.exam_id nao tinha
-- FK. Consentimento guarda IP e fingerprint — dados sensiveis (LGPD Art. 43, III).
-- Quando um exame era deletado, a linha de consentimento ficava orfa para sempre:
-- o DELETE /api/profile/{user_id} (exclusao em cascata) nao a alcanca via FK, e
-- nenhum purge existia. Agora a integridade e garantida pelo banco.
--
-- A coluna ja existe e e NOT NULL; so adicionamos a chave estrangeira. Antes de
-- criar, removemos eventuais linhas orfas (exam_id sem correspondente) para que
-- o ALTER nao falhe — consentimento orfao e lixo sem valor juridico, e o
-- registro de auditoria principal (tombstone) continua existindo.
--
-- CASCADE: apagar o exame apaga o consentimento associado (alinhado com o
-- DELETE em cascata do perfil).

DELETE FROM exam_cloud_consents c
WHERE NOT EXISTS (SELECT 1 FROM exams e WHERE e.id = c.exam_id);

ALTER TABLE exam_cloud_consents
    ADD CONSTRAINT fk_exam_cloud_consents_exam
    FOREIGN KEY (exam_id) REFERENCES exams(id) ON DELETE CASCADE;

INSERT INTO schema_migrations(version) VALUES ('013_exam_cloud_consents_fk') ON CONFLICT DO NOTHING;
