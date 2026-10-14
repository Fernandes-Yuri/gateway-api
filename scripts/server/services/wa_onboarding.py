"""Orientação institucional variada; não consulta nem registra dados clínicos."""
import re
from collections import OrderedDict
from time import monotonic
from services.wa_channel_privacy import norm, phone_hash

DOWNLOAD_URL = 'https://bragasaude.online/app-download.html'
_counters = OrderedDict()


def clear(phone):
    _counters.pop(phone_hash(phone), None)


def variant(phone, kind, options):
    key = phone_hash(phone)
    now = monotonic()
    for stale in [k for k, (seen, _) in _counters.items() if now - seen >= 1800]:
        del _counters[stale]
    counts = _counters.get(key, (now, {}))[1]
    index = counts.get(kind, 0)
    counts[kind] = index + 1
    _counters[key] = (now, counts)
    _counters.move_to_end(key)
    while len(_counters) > 1000:
        _counters.popitem(last=False)
    return options[index % len(options)]


def unlinked_reply(text, phone):
    text = norm(text)
    if re.search(r'\b(baixar|baixo|baixa|download|instalar|instalo|play store)\b', text):
        return variant(phone, 'download', (
            f'Você pode baixar o Braga Saúde para Android pela página oficial: {DOWNLOAD_URL}\nDepois de instalar e entrar na sua conta, use “Vincular WhatsApp” no app.',
            f'Para instalar no Android, abra {DOWNLOAD_URL} e siga as instruções da página. Com o app instalado, entre na conta e toque em “Vincular WhatsApp”.',
            f'O download oficial para Android fica aqui: {DOWNLOAD_URL}\nA página orienta a instalação. Depois vincule este número pelo aplicativo.'))
    if re.search(r'\b(vincul\w*|codigo|totp)\b', text):
        return variant(phone, 'link', (
            f'No aplicativo Braga Saúde, entre na sua conta e toque em “Vincular WhatsApp”. Use aqui o código gerado pelo app. Ainda não instalou? {DOWNLOAD_URL}',
            f'O vínculo começa no app: abra “Vincular WhatsApp” e envie aqui o código exibido. Se precisar instalar primeiro, use a página oficial: {DOWNLOAD_URL}',
            f'Para conectar este número à sua conta, gere o código em “Vincular WhatsApp”, no Braga Saúde, e envie nesta conversa. Download para Android: {DOWNLOAD_URL}'))
    if re.search(r'\b(ajud\w*|funciona|faz|consigo|pode fazer)\b', text):
        return variant(phone, 'help', (
            f'O app reúne seus registros, histórico e lembretes. Com o número vinculado, posso ajudar por aqui com consultas e registros confirmados. Para começar no Android: {DOWNLOAD_URL}',
            f'Eu apoio seu uso do Braga Saúde: após vincular, posso consultar registros e preparar medições para confirmação. Rotinas e lembretes ficam no app. Instalação: {DOWNLOAD_URL}',
            f'Você acompanha sua saúde e configura lembretes no app; o WhatsApp facilita consultas e registros quando estiver vinculado. Comece pela página oficial: {DOWNLOAD_URL}'))
    return variant(phone, 'unlinked', (
        f'Para consultar ou registrar dados de saúde aqui, vincule este número à sua conta pelo app, em “Vincular WhatsApp”. Se ainda não tem o Braga Saúde no Android: {DOWNLOAD_URL}',
        f'Este número ainda não está vinculado. Abra o Braga Saúde e use “Vincular WhatsApp” para liberar seus registros nesta conversa. Precisa instalar? {DOWNLOAD_URL}',
        f'Os registros ficam protegidos até você confirmar o vínculo pelo aplicativo. Entre na conta e toque em “Vincular WhatsApp”. Download oficial para Android: {DOWNLOAD_URL}'))


def preference_reply(text, phone):
    text = norm(text)
    if re.search(r'\b(cham\w*|apelido|nome)\b', text):
        return variant(phone, 'nickname', (
            'Aqui uso o primeiro nome da sua conta e não salvo apelidos. Você pode conferir seu perfil no aplicativo Braga Saúde.',
            'No WhatsApp, identifico você pelo primeiro nome cadastrado. Apelidos não ficam guardados neste assistente; seu perfil está no app.'))
    if re.search(r'\b(resposta\w*|curt\w*|long\w*|detalh\w*)\b', text):
        return variant(phone, 'style', (
            'Vou procurar ser claro e direto. Não salvo preferências de resposta neste assistente WhatsApp.',
            'Posso responder com objetividade nesta conversa, sem guardar uma preferência permanente no WhatsApp.'))
    return variant(phone, 'memory', (
        'Não mantenho apelidos nem preferências salvas no WhatsApp. Seus registros confirmados e configurações ficam na sua conta do aplicativo.',
        'As configurações pessoais permanecem no app; aqui não salvo preferências ou apelidos. Uso apenas o contexto recente da conversa para dar continuidade.'))


def linked_download_reply(phone):
    return variant(phone, 'linked-download', (
        f'O Braga Saúde para Android pode ser baixado pela página oficial: {DOWNLOAD_URL}. Lá você encontra as instruções de instalação.',
        f'Para instalar o app no Android, acesse {DOWNLOAD_URL} e siga as orientações da página oficial.',
        f'Este é o endereço oficial de download para Android: {DOWNLOAD_URL}. O app reúne seu histórico e as configurações da sua rotina.'))
