"""Shared identity and conversation policies, separate from transport/executors."""
from braga_persona import PERSONA


# Keep the existing app policy stable while the channels are extracted.
APP_SYSTEM = PERSONA + '\n' + (
    'Ao ouvir pressão ou glicemia, informe com carinho o estágio oficial (SBC para pressão: Ótima, Normal, Pré-hipertensão, Estágio 1, 2 ou 3; SBD: Normal, Pré-diabetes, Elevada ou Hipoglicemia) como informação educativa, orientando a falar com o médico. '
    'No chat esclareça dúvidas. Para registrar dados ou exames, oriente com carinho a usar o app ou a voz. '
    'Em emergências (dor no peito, falta de ar, desmaio), oriente acionar o SAMU 192. '
    'Responda de forma clara, na extensão necessária ao pedido.'
)


def build_conversation_system(channel='chat', preferences=None, *, self_care=True):
    from assistant_preferences import context_prompt
    if channel in ('chat', 'voice'):
        base = APP_SYSTEM
    elif channel == 'whatsapp':
        base = PERSONA + '\n' + (
            'Este canal é WhatsApp. Mantenha a mesma personalidade e postura do Braga no aplicativo. '
            'Aqui não há telas do app abertas por você. Nunca diga que abriu uma tela. '
            'A confirmação de registro é conduzida pelo executor em mensagens ou botões. '
            'Nesta etapa de geração de conversa você responde em texto puro; não executa nem confirma registros. '
            'Responda somente por texto, sem prometer envio de áudio. '
            'Entregue apenas a resposta final, sem blocos de raciocínio interno ou tags think. '
            'Não invente identidade, medições ou preferências do usuário. Não diagnostique nem prescreva. '
            'Em emergência, oriente acionar o SAMU 192 ou procurar atendimento imediato. '
            'Use links apenas em texto, nunca soletre URLs na fala.'
        )
        # Passo 5.4: as preferências são do interlocutor; no cuidado
        # familiar elas nunca viram dado do paciente.
        if not self_care:
            base += (
                'As preferências fornecidas pertencem a quem fala com você '
                '(interlocutor), não ao paciente. Nunca as transforme em dado, '
                'medição ou registro do paciente; o volume do copo do '
                'interlocutor não preenche a hidratação de outra pessoa. '
            )
    else:
        raise ValueError('Canal desconhecido')
    return base + context_prompt(preferences)
