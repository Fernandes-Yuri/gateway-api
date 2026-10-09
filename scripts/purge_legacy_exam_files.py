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
    purge(Path(os.getenv("STORAGE_DIR", "/home/ec2-user/braga-gateway/data/storage")))
    print("Expurgo do armazenamento legado de exames concluído.")
