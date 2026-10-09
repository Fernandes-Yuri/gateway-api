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
    roots = {project / "data/storage", project / "storage"}
    if os.getenv("STORAGE_DIR"):
        roots.add(Path(os.environ["STORAGE_DIR"]))
    for storage in roots:
        purge(storage)
    for module in ("exams_api", "clinical_references"):
        for bytecode in (project / "app/__pycache__").glob(f"{module}.*.pyc"):
            bytecode.unlink()
        (project / "app" / f"{module}.pyc").unlink(missing_ok=True)
    print("Expurgo do armazenamento legado de exames concluído.")
