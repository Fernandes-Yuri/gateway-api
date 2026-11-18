"""Shared identity and conversation policies, separate from transport/executors."""
from braga_persona import PERSONA
from core.product_scope import PRODUCT_SCOPE
from services.emergency_reply import SAMU_URL, UPA_URL


# Linguagem compartilhada; os contratos de transporte e ações permanecem iguais.
APP_SYSTEM = PERSONA + '\n' + PRODUCT_SCOPE + '\n' + (
    'Referências a pedido do usuário: SBC/SBH/SBN 2025 (consultório, adultos) e SBD; informação educativa geral. '
    'Não classifique a pessoa em um estágio clínico. '
    'Nunca invente ou sugira medicamento, dose ou horário. '
    'Ao ser citado, reconheça o que você escreveu. '
    f'Socorro: SAMU 192, {SAMU_URL}, {UPA_URL}; não avalie risco nem prometa acionamento. '
    'Preserve negações, bebidas e volumes; hipóteses não são relatos. Não ofereça registros por texto, alimentação ou compras.'
)


def build_conversation_system(channel='chat', preferences=None, *, self_care=True, account_name=None):
    from assistant_preferences import context_prompt
    if channel not in ('chat', 'voice'):
        raise ValueError('Canal desconhecido')
    return APP_SYSTEM + context_prompt(preferences)
