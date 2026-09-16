"""Export legacy VIP entries without deleting them. Run manually on the server.

Creates a private JSON backup and a psql COPY import file; never prints contacts.
Import: sudo docker exec -i bragasaude-postgres psql -X -v ON_ERROR_STOP=1
        -U bragasaude_user -d bragasaude < <output>/import.sql
"""
import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from vip_api import normalize_signup


def write_export(documents, destination):
    os.umask(0o077)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    records = [{'id': doc.id, 'data': doc.to_dict()} for doc in documents]
    # Preserve ALL entries, including invalid ones, before attempting conversion.
    (destination / 'firestore-vip-backup.json').write_text(
        json.dumps(records, ensure_ascii=False, default=str), encoding='utf-8')
    valid, invalid = [], []
    for record in records:
        data = record['data']
        try:
            name, email = normalize_signup(data)
        except ValueError:
            invalid.append(record)
            continue
        timestamp = data.get('createdAt')
        valid.append(dict(fullName=name, email=email,
                          createdAt=timestamp.isoformat() if isinstance(timestamp, datetime) and timestamp.tzinfo else None))
    (destination / 'revisar-invalidos.json').write_text(json.dumps(invalid, ensure_ascii=False, default=str), encoding='utf-8')
    with (destination / 'import.sql').open('w', encoding='utf-8', newline='\n') as output:
        output.write('BEGIN;\nCREATE TEMP TABLE vip_import(payload jsonb) ON COMMIT DROP;\nCOPY vip_import(payload) FROM STDIN;\n')
        for record in valid:
            output.write(json.dumps(record, ensure_ascii=True).replace('\\', '\\\\') + '\n')
        output.write('\\.\n')
        output.write("""INSERT INTO vip_signups(full_name,email,created_at,source)
SELECT payload->>'fullName', payload->>'email',
       coalesce((payload->>'createdAt')::timestamptz, now()), 'firestore-import'
FROM vip_import ON CONFLICT(email) DO NOTHING;
SELECT count(*) AS registros_lidos_do_backup FROM vip_import;
COMMIT;
""")
    return len(records), len(valid), len(invalid)


if __name__ == '__main__':
    import firebase_admin
    from firebase_admin import credentials, firestore
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--credentials', default=os.getenv('FIREBASE_SERVICE_ACCOUNT_PATH', '/home/yurifjs/projetos/braga-gateway/serviceAccountKey.json'))
    args = parser.parse_args()
    firebase_admin.initialize_app(credentials.Certificate(args.credentials))
    counts = write_export(firestore.client().collection('vip-signups').stream(), args.output)
    print('Exportados: %d; válidos: %d; para revisão: %d. Nenhum documento foi apagado.' % counts)
