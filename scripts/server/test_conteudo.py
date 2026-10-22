"""Testes unitários do verificador de conteúdo público. Sem rede, sem banco."""
import asyncio
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services import conteudo_verificacao as cv  # noqa: E402


def _limpar_env(monkeypatch):
    monkeypatch.delenv('CONTEUDO_URLS', raising=False)
    monkeypatch.delenv('CONTEUDO_REF', raising=False)
    monkeypatch.delenv('CONTEUDO_VERIFICACAO_ENABLED', raising=False)
    monkeypatch.delenv('CONTEUDO_INTERVALO_SEG', raising=False)
    monkeypatch.delenv('CONTEUDO_RETENCAO_DIAS', raising=False)


def test_habilitado_ligado_e_desligado(monkeypatch):
    _limpar_env(monkeypatch)
    monkeypatch.setenv('CONTEUDO_VERIFICACAO_ENABLED', 'true')
    assert cv.habilitado() is True
    monkeypatch.setenv('CONTEUDO_VERIFICACAO_ENABLED', 'não')
    assert cv.habilitado() is False


def test_urls_padrao_quando_env_vazio(monkeypatch):
    _limpar_env(monkeypatch)
    urls = cv.urls_alvo()
    assert len(urls) == 3 and all(u.startswith('https://') for u in urls)


def test_urls_do_env(monkeypatch):
    _limpar_env(monkeypatch)
    monkeypatch.setenv('CONTEUDO_URLS', 'https://a.example/um\n\nhttps://b.example/dois')
    assert cv.urls_alvo() == ['https://a.example/um', 'https://b.example/dois']


def test_hash_normaliza_apenas_quebras(monkeypatch):
    _limpar_env(monkeypatch)
    html_crlf = '<html>a\r\nb\rc</html>'
    html_lf = '<html>a\nb\nc</html>'
    hash_a = cv.hash_corpo('index.html', html_crlf.encode())
    hash_b = cv.hash_corpo('index.html', html_lf.encode())
    assert hash_a == hash_b  # quebra de linha de borda não muda o conteúdo

    json_a = cv.hash_corpo('hospitais.json', '{"x": 1}\r\n'.encode())
    json_b = cv.hash_corpo('hospitais.json', '{"x": 1}\n'.encode())
    assert json_a != json_b  # JSON é comparado byte a byte (catálogo é assinado no build)

    diferente = cv.hash_corpo('index.html', (html_lf + '!').encode())
    assert diferente != hash_a


def test_referencias_ignora_env_invalida(monkeypatch):
    _limpar_env(monkeypatch)
    monkeypatch.setenv('CONTEUDO_REF', 'não é json')
    assert cv.referencias() == {}
    monkeypatch.setenv('CONTEUDO_REF', '{"https://x.example/": "abc", "y": 12}')
    assert cv.referencias() == {}  # hash com menos de 64 hex não é aceito
    h64 = 'x' * 64
    monkeypatch.setenv('CONTEUDO_REF', f'{{"https://x.example/": "{h64}"}}')
    assert cv.referencias() == {'https://x.example/': h64}


async def _resposta(texto, status=200):
    pedido = httpx.Request('GET', 'https://x.example/')
    return httpx.Response(status, text=texto, request=pedido)


async def test_coletar_marca_expected_e_unexpected(monkeypatch):
    _limpar_env(monkeypatch)
    corpo = '<html>estavel</html>\n'
    hash = cv.hash_corpo('index.html', corpo.encode())
    monkeypatch.setenv('CONTEUDO_REF', f'{{"https://x.example/": "{hash}"}}')

    dados = await cv.coletar_uma(ok_cliente(_resposta(corpo)), 'https://x.example/')
    assert dados['match'] == 'expected'

    dados = await cv.coletar_uma(ok_cliente(_resposta(corpo + 'x')), 'https://x.example/')
    assert dados['match'] == 'unexpected'

    dados = await cv.coletar_uma(ok_cliente(_resposta('robô', status=503)), 'https://x.example/')
    assert dados['match'] == 'unknown'


def ok_cliente(resposta):
    def handler(pedido):
        r = resposta
        r.request = pedido if r.request is None else r.request
        return r
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_hash_corresponde_a_sha256_conhecido():
    import hashlib
    corpo = b'conteudo-de-referencia'
    assert cv.hash_corpo('index.html', corpo) == hashlib.sha256(corpo).hexdigest()


def test_intervalo_e_retencao_padrao(monkeypatch):
    _limpar_env(monkeypatch)
    assert cv.intervalo_segundos() == 900
    assert cv.dias_retencao() == 90
    monkeypatch.setenv('CONTEUDO_INTERVALO_SEG', 'lixo')
    monkeypatch.setenv('CONTEUDO_RETENCAO_DIAS', '-5')
    assert cv.intervalo_segundos() == 900
    assert cv.dias_retencao() == 1
