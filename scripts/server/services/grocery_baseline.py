"""Baseline de cotações públicas CEAGESP, com mapeamento explícito e auditável."""
import asyncio
import calendar
import json
import re
import statistics
import unicodedata
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path

URL = "https://ceagesp.gov.br/cotacoes/"
MAP_PATH = Path(__file__).resolve().parent.parent / "grocery_source_map.json"
GROUPS = ("FRUTAS", "LEGUMES", "VERDURAS", "DIVERSOS", "PESCADOS")
PROCON_INDEX = "https://www.procon.sp.gov.br/pesquisas/"
MONTHS = {"JANEIRO": 1, "FEVEREIRO": 2, "MARCO": 3, "ABRIL": 4, "MAIO": 5, "JUNHO": 6,
          "JULHO": 7, "AGOSTO": 8, "SETEMBRO": 9, "OUTUBRO": 10, "NOVEMBRO": 11, "DEZEMBRO": 12}


def normalizar(value):
    return " ".join(unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().upper().split())


class QuoteTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.cell = None
        self.row = []
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "table" and "contacao_lista" in dict(attrs).get("class", "").split():
            self.active = True
        if self.active and tag == "tr":
            self.row = []
        if self.active and tag == "td":
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if not self.active:
            return
        if tag == "td" and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if tag == "tr" and self.row:
            self.rows.append(self.row)
        if tag == "table":
            self.active = False


def _number(text):
    number = Decimal(text.replace(".", "").replace(",", "."))
    if not number.is_finite() or number <= 0:
        raise ValueError("Cotação inválida")
    return number


def parse_table(html, group, date):
    parser = QuoteTable()
    parser.feed(html)
    expected = ["PRODUTO", "CLASSIFICACAO", "UNI/PESO", "MENOR", "COMUM", "MAIOR", "QUILO"]
    if not any([normalizar(cell) for cell in row] == expected for row in parser.rows):
        raise ValueError("Layout da CEAGESP mudou")
    if not any(date in " ".join(row) and group in " ".join(row) for row in parser.rows):
        raise ValueError("Data ou grupo diferente da consulta")
    rows, errors = [], 0
    for cells in parser.rows:
        if len(cells) != 7 or normalizar(cells[0]) == "PRODUTO":
            continue
        try:
            low, common, high, weight = map(_number, cells[3:])
            if not low <= common <= high:
                raise ValueError("Faixa inválida")
            rows.append({"produto": normalizar(cells[0]), "classe": cells[1], "unidade": normalizar(cells[2]),
                         "min": str(low), "avg": str(common), "max": str(high), "peso_kg": str(weight),
                         "grupo": group, "data": datetime.strptime(date, "%d/%m/%Y").date().isoformat(), "url": URL})
        except (ValueError, ArithmeticError):
            errors += 1
    if not rows:
        raise ValueError("Tabela de cotações vazia")
    return rows, errors


async def fetch_quotes(client, now):
    response = await client.get(URL, timeout=10)
    response.raise_for_status()
    match = re.search(r"var\s+Grupos\s*=\s*(\{.*?\});", response.text, re.S)
    if not match:
        raise ValueError("Calendário de cotações ausente")
    groups = json.loads(match.group(1))
    quotes, errors = [], 0
    for group in GROUPS:
        dates = [datetime.strptime(d, "%d/%m/%Y").date() for d in (groups.get(group) or [])]
        dates = [d for d in dates if 0 <= (now.date() - d).days <= 7]
        if not dates:
            continue
        date = max(dates).strftime("%d/%m/%Y")
        try:
            await asyncio.sleep(.5)
            response = await client.post(URL, data={"cot_grupo": group, "cot_data": date}, timeout=10)
            response.raise_for_status()
            rows, invalid = parse_table(response.text, group, date)
            quotes.extend(rows)
            errors += invalid
        except Exception:
            errors += 1
            print(f"[grocery_baseline] Falha na consulta pública do grupo {group}")
    return quotes, errors


def parse_procon(content, url, now):
    import pymupdf
    with pymupdf.open(stream=content, filetype="pdf") as document:
        for page in document:
            text = normalizar(page.get_text(sort=True))
            if "VARIACAO MENSAL DO CUSTO MEDIO DA CESTA BASICA" not in text:
                continue
            period = re.search(r"CESTA BASICA\s+(\w+)/(\d{2})", text)
            if not period or period[1] not in MONTHS:
                raise ValueError("Período do relatório ausente")
            year, month = 2000 + int(period[2]), MONTHS[period[1]]
            date = datetime(year, month, calendar.monthrange(year, month)[1]).date()
            if not 0 <= (now.date() - date).days <= 90:
                raise ValueError("Relatório fora da janela de referência")
            match = re.search(r"FEIJAO CARIOQUINHA \(KG\)\s+(\d+,\d{2})\s+(\d+,\d{2})\s+[-\d,.]+%", text)
            if not match:
                raise ValueError("Linha do feijão ausente")
            price = str(_number(match[2]))
            return [{"produto": "FEIJAO CARIOQUINHA (KG)", "classe": "Varejo PROCON-SP/DIEESE",
                     "unidade": "KG", "min": price, "avg": price, "max": price,
                     "peso_kg": "1", "grupo": "PROCON", "data": date.isoformat(), "url": url}]
    raise ValueError("Tabela mensal não encontrada")


async def fetch_procon(client, now):
    response = await client.get(PROCON_INDEX, timeout=10)
    response.raise_for_status()
    # Só URLs de relatórios mensais da própria instituição; nunca seguir links externos.
    links = re.findall(r'href=["\x27](https://www\.procon\.sp\.gov\.br/wp-content/uploads/\d{4}/\d{2}/CB[^"\x27]+\.pdf)["\x27]', response.text)
    links = sorted(set(links), reverse=True)
    for url in links[:4]:
        if "anual" in url.lower() and not ("compar" in url.lower() or "companual" in url.lower()):
            continue
        try:
            response = await client.get(url, timeout=10)
            response.raise_for_status()
            if len(response.content) > 10 * 1024 * 1024:
                raise ValueError("Relatório excede 10 MiB")
            return parse_procon(response.content, url, now)
        except Exception:
            continue
    raise ValueError("Relatório mensal recente não disponível")


def build_baseline(items, mappings, quotes):
    entries, missing = [], []
    maps = {row["food_name"]: row for row in mappings}
    for item in items:
        mapping = maps.get(item["food_name"], {})
        identifiers = mapping.get("ceagesp", []) + mapping.get("procon", [])
        unit = mapping.get("unidade")
        quantity = Decimal(str(mapping.get("quantidade", 1)))
        factor = Decimal(str(mapping.get("fator_varejo", 1.6)))
        candidates = []
        for quote in quotes:
            if quote["produto"] not in identifiers:
                continue
            # Só aceitar mesma unidade. KG -> unidade exige peso individual documentado.
            if quote["unidade"] == unit:
                multiplier = quantity * factor
            elif unit == "KG" and quote["unidade"] in {"UN", "ENG", "PCT", "MC", "BJ"}:
                multiplier = quantity * factor / Decimal(quote["peso_kg"])
            elif unit in {"UN", "MC"} and quote["unidade"] == "ENG":
                classification = normalizar(quote["classe"])
                suffix = r"(?:UNIDADES|CABECAS)" if unit == "UN" else r"(?:MACOS|MCS)"
                match = re.search(r"(\d+)(?:/(\d+))?\s+" + suffix, classification)
                if not match:
                    continue
                count = (Decimal(match[1]) + Decimal(match[2] or match[1])) / 2
                multiplier = quantity * factor / count
            else:
                continue
            candidates.append((quote, multiplier))
        if not candidates:
            missing.append(item["food_name"])
            continue
        latest = max(q["data"] for q, _ in candidates)
        candidates = [(q, f) for q, f in candidates if q["data"] == latest]
        values = [Decimal(q["avg"]) * f for q, f in candidates]
        # A mediana evita que uma classificação extrema domine o preço-base.
        price = statistics.median(values).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if not Decimal("0.01") <= price <= Decimal("99999999.99"):
            raise ValueError("Preço convertido fora dos limites")
        entries.append({"food_name": item["food_name"], "search_term": item["search_term"], "price": str(price),
                        "data": latest, "unidade": unit, "quantidade": str(quantity),
                        "fator_varejo": str(factor), "cotacoes": [q for q, _ in candidates]})
    return entries, missing


async def fetch_baseline(client, items, now):
    try:
        mappings = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        quotes, errors = [], 0
        try:
            ceagesp, invalid = await fetch_quotes(client, now)
            quotes.extend(ceagesp)
            errors += invalid
        except Exception:
            errors += 1
            print("[grocery_baseline] CEAGESP indisponível")
        try:
            quotes.extend(await fetch_procon(client, now))
        except Exception:
            errors += 1
            print("[grocery_baseline] PROCON indisponível")
        entries, _ = build_baseline(items, mappings, quotes)
        return {row["food_name"]: row for row in entries}, errors
    except Exception:
        print("[grocery_baseline] Fonte pública indisponível; preços existentes preservados")
        return {}, 1
