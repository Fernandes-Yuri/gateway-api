"""Verificação de integridade do conteúdo público das clínicas.

Coleta apenas recursos públicos (somente GET), grava snapshots hash no banco
e compara com o hash de referência configurado por variável de ambiente.
Divergência é registrado no snapshot (match_state), nunca bloqueia a coleta.
Nenhum dado privado de paciente ou credencial passa por aqui.
"""
import asyncio
import hashlib
import json
import os

import httpx

INTERVALO_PADRAO = 900
RETENCAO_PADRAO_DIAS = 90

URLS_PADRAO = (
    'https://clinicas.bragasaude.online/',
    'https://clinicas.bragasaude.online/data/hospitais.json',
    'https://clinicas.bragasaude.online/assets/faq.js',
)

# Pedaços que variam por borda/CDN ou por render, sem mudar o conteúdo real.
# (reservado; normalização atual só trata quebras de linha em HTML/JS)


def habilitado():
    return os.getenv('CONTEUDO_VERIFICACAO_ENABLED', '').lower() in ('1', 'true', 'yes')


def urls_alvo():
    bruto = os.getenv('CONTEUDO_URLS', '').strip()
    if not bruto:
        return list(URLS_PADRAO)
    alvo = [linha.strip() for linha in bruto.splitlines() if linha.strip()]
    return alvo or list(URLS_PADRAO)


def normalizar(nome_recurso, corpo_bytes):
    """Normaliza o corpo antes do hash: apenas diferenças de borda, não de conteúdo."""
    if not nome_recurso.endswith('.json'):
        texto = corpo_bytes.decode('utf-8', errors='replace')
        texto = texto.replace('\r\n', '\n').replace('\r', '\n')
        return texto.encode('utf-8')
    return corpo_bytes


def hash_corpo(nome_recurso, corpo_bytes):
    return hashlib.sha256(normalizar(nome_recurso, corpo_bytes)).hexdigest()


def _cabecalhos_relevantes(pares):
    # Guarda apenas nome de servidor/edge e data; nada de cookies ou rastreio.
    mantidos = ('server', 'cf-ray', 'x-served-by', 'x-firefox', 'x-firebase-request-hash')
    return {k: v for k, v in pares if k.lower() in mantidos}


def referencias():
    """Hashes esperados por URL: env CONTEUDO_REF contém JSON {url: sha256}."""
    bruto = os.getenv('CONTEUDO_REF', '').strip()
    if not bruto:
        return {}
    try:
        mapa = json.loads(bruto)
    except ValueError:
        return {}
    return {str(k): str(v).lower() for k, v in mapa.items()
            if isinstance(k, str) and isinstance(v, str) and len(str(v)) == 64}


async def coletar_uma(http, url):
    resp = await http.get(url, follow_redirects=False, timeout=20)
    corpo = resp.content
    nome = urlsplit_nome(url)
    dados = {
        'url': url,
        'hash': hash_corpo(nome, corpo),
        'body': corpo,
        'headers': _cabecalhos_relevantes(list(resp.headers.items())),
        'status': resp.status_code,
        'tamanho': len(corpo),
    }
    ref = referencias().get(url, '')
    if not ref or resp.status_code != 200:
        dados['match'] = 'unknown'
    else:
        dados['match'] = 'expected' if ref.lower() == dados['hash'] else 'unexpected'
    return dados


def urlsplit_nome(url):
    import urllib.parse
    caminho = urllib.parse.urlsplit(url).path
    return caminho.rsplit('/', 1)[-1] or 'index.html'


async def gravar(pool, dados):
    async with pool.acquire() as conn:
        await conn.execute(
            'INSERT INTO conteudo_snapshots (url,hash_sha256,body,headers,http_status,match_state,tamanho) '
            'VALUES ($1,$2,$3,$4,$5,$6,$7)',
            dados['url'], dados['hash'], dados['body'],
            json.dumps(dados['headers']), dados['status'], dados['match'], dados['tamanho'])


async def aplicar_retencao(pool, dias):
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM conteudo_snapshots WHERE coletado_em < NOW() - make_interval(days => $1)", dias)


async def ciclo(pool, http):
    resultado = []
    for url in urls_alvo():
        try:
            dados = await coletar_uma(http, url)
        except Exception as erro:
            print(f'[conteúdo] falha coletando {url}: {type(erro).__name__}')
            continue
        await gravar(pool, dados)
        resultado.append(dados)
    await aplicar_retencao(pool, dias_retencao())
    return resultado


def dias_retencao():
    try:
        return max(1, int(os.getenv('CONTEUDO_RETENCAO_DIAS', str(RETENCAO_PADRAO_DIAS))))
    except ValueError:
        return RETENCAO_PADRAO_DIAS


def intervalo_segundos():
    try:
        return max(60, int(os.getenv('CONTEUDO_INTERVALO_SEG', str(INTERVALO_PADRAO))))
    except ValueError:
        return INTERVALO_PADRAO


async def loop(pool):
    async with httpx.AsyncClient(headers={'User-Agent': 'BragaSaudeConteudo/1.0 (+verificador de integridade)'}) as http:
        while True:
            try:
                await ciclo(pool, http)
            except Exception as erro:
                print(f'[conteúdo] ciclo falhou: {type(erro).__name__}')
            await asyncio.sleep(intervalo_segundos())


def start(pool):
    return [asyncio.create_task(loop(pool))]
