"""Personalidade compartilhada com os assets Android, sem duplicar o texto."""
from pathlib import Path

PERSONA = (Path(__file__).parent / "persona" / "braga.txt").read_text(encoding="utf-8").strip()
