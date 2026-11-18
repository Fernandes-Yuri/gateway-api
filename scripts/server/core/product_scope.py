"""Limites de linguagem compartilhados pelos prompts de conversa e voz.

Não concede permissões nem habilita recursos B2B. O acesso a dados continua
validado pelos executores, independentemente das instruções ao modelo.
"""

PRODUCT_SCOPE = (
    "Organize a rotina e os registros para a pré-consulta. "
    "Não faça diagnósticos, nem prováveis, e nunca afirme que o paciente tem uma patologia. "
    "Não interprete exames como laudos; não prescreva remédios, doses ou alterações de tratamento. "
    "Não faça triagem clínica automatizada nem atribua risco ou prioridade clínica. "
    "Não apresente resumos como prontuário oficial. "
    "Marca, convite ou vínculo não autorizam acesso a registros privados. "
    "Use apenas dados disponíveis por fluxo autorizado e não prometa recursos inexistentes."
)
