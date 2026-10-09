"""Expurgo do diretório legado de exames, somente pelo pipeline de deploy."""
import os
import shutil
from pathlib import Path


def purge(storage: Path) -> None:
    target = storage / "exams"
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


if __name__ == "__main__":
    project = Path(os.getenv("BRAGA_PROJECT_ROOT", str(Path(__file__).resolve().parents[1])))
    purge(Path(os.getenv("STORAGE_DIR", str(project / "data/storage"))))
    for module in ("exams_api", "clinical_references"):
        for bytecode in (project / "app/__pycache__").glob(f"{module}.*.pyc"):
            bytecode.unlink()
        (project / "app" / f"{module}.pyc").unlink(missing_ok=True)
    print("Expurgo do armazenamento legado de exames concluído.")
