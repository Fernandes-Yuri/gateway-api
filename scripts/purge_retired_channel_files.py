"""Expurgo idempotente de arquivos do assistente encerrado, pelo deploy parado."""
import json
import os
from pathlib import Path
import tempfile


def rewrite(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_symlink() or not path.is_file():
        raise ValueError('Arquivo de dados inválido para expurgo')
    removed = 0
    temp_name = None
    try:
        with path.open(encoding='utf-8') as source, tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=path.parent, delete=False) as target:
            temp_name = target.name
            os.chmod(temp_name, 0o600)
            for line in source:
                if not line.strip():
                    target.write(line)
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    raise ValueError('Arquivo JSONL inválido; expurgo não concluído') from None
                if not isinstance(record, dict):
                    raise ValueError('Registro JSONL inválido; expurgo não concluído')
                if any(str(record.get(key, '')).lower().startswith('whatsapp') for key in ('source', 'channel')):
                    removed += 1
                else:
                    target.write(line)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
    return removed


def purge(project: Path) -> int:
    # Estes caminhos eram exclusivos do outbox e dos dois túneis antigos.
    for root in (project, project/'app'):
        for name in ('webhook_outbox.log', '.whatsapp_tunnel_url'):
            target = root/name
            if target.is_dir() and not target.is_symlink():
                raise ValueError('Caminho legado inválido para expurgo')
            target.unlink(missing_ok=True)
    paths = {project/'llm_trace.jsonl', project/'app/llm_trace.jsonl',
             project/'dataset_braga.jsonl', project/'app/dataset_braga.jsonl',
             project/'data/dataset/dataset_braga.jsonl'}
    for key in ('TRACE_PATH','DATASET_PATH'):
        if os.getenv(key):
            path = Path(os.environ[key])
            paths.add(path if path.is_absolute() else project/path)
    return sum(rewrite(path) for path in sorted(paths))


if __name__ == '__main__':
    project = Path(os.getenv('BRAGA_PROJECT_ROOT', str(Path(__file__).resolve().parents[1])))
    from dotenv import load_dotenv
    for candidate in (project/'.env', project.parent/'.env', project/'app/.env'):
        if candidate.exists():
            load_dotenv(candidate)
    count = purge(project)
    print(f'Expurgo de arquivos do canal encerrado concluído: {count} registros removidos.')
