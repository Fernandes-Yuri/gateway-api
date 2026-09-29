"""Nome público separado do nome civil e preservado pelo sync de clientes antigos."""
import unicodedata


def normalize_nickname(value: str) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    if any(not (c.isalnum() or c in " .'-") for c in value):
        raise ValueError("Use letras, números, espaços, ponto, apóstrofo ou hífen.")
    value = " ".join(value.split())
    if value and (not 2 <= len(value) <= 24 or not any(c.isalnum() for c in value)):
        raise ValueError("O nome comunitário deve ter de 2 a 24 caracteres.")
    return value
