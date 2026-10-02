-- Vincula anexos de receitas ao paciente, sem confiar no nome do arquivo.
CREATE TABLE IF NOT EXISTS prescription_files (
    filename TEXT PRIMARY KEY,
    patient_id VARCHAR(128) NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS prescription_files_patient_idx ON prescription_files(patient_id);
