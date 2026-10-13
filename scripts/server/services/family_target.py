"""Resolução do familiar alvo no WhatsApp (PR4, passo 4.1).

O cuidador escreve "como está a mamãe?" ou "qual a pressão do papai?". O alvo
nunca é escolhido pelo modelo: a conta alvo vem dos vínculos familiares ATIVOS
da conta autenticada, lidos no banco.

Regras (PLANO-APP-WHATSAPP §4.1):
- Sem vínculo ativo -> nenhum alvo; orienta o app.
- Vínculo único -> só resolve sem perguntar se não houver alvo incompatível.
- Vários vínculos, ou nome ambíguo -> pede a escolha; NUNCA auto-seleciona o
  primeiro da lista.
- Consulta pessoal ("minha pressão") segue na própria conta: este serviço só
  resolve o alvo quando a fala menciona outra pessoa.
- O alvo é válido somente no contexto da operação; vínculo revogado é
  revalidado a cada uso (passo 4.2).

O alvo resolvido é devolvido no OperationContext (patient_id != actor_id); a
revalidação de permissão fica em validate_actor (passo 1.3), que reutiliza
require_patient_permission.
"""
import re
import unicodedata
from typing import Any, List, Optional, Tuple

from services.operations import OperationContext, OperationResult
from services.display_names import first_name, choice_labels

# Marcadores de que a fala é sobre OUTRA pessoa (não autocuidado).
_OTHER_HINT = re.compile(
    r"\b(?:mae|pai|filha|filho|irma|irmao|tia|tio|avo|sobrinha|sobrinho|esposa|esposo|marido|conjuge)\b|"
    r"\bmamae\b|\bminha mae|\bmaezinha|\bmeu pai|\bpapai|\bminha filha|\bmeu filho|"
    r"\bminha irma|\bmeu irmao|\bminha tia|\bmeu tio|\bminha avo|\bmeu avo|"
    r"\bvovo|\bvov|\bvovozinha|\bvovozi|\bminha sobrinha|\bmeu sobrinho|"
    r"\bminha esposa|\bminha mulher|\bmeu marido|\bmeu conjuge|\bfamiliar|"
    r"\bpaciente\b|\bele\b|\bela\b|\bdela\b|\bdele\b"
)


def _norm(text: str) -> str:
    no_acc = unicodedata.normalize("NFD", text or "")
    no_acc = "".join(c for c in no_acc if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", no_acc.lower()).strip()


def _explicit_person_name(text: str) -> Optional[str]:
    stopwords = {"hoje", "ontem", "amanha", "dia", "semana", "mes", "ano", "mim", "voce",
                "que", "qual", "quais", "quando", "como", "quanto", "quantos", "quantas",
                "ultima", "ultimo", "ultimas", "ultimos", "primeira", "primeiro",
                "manha", "tarde", "noite", "madrugada", "hora", "horas",
                "agua", "pressao", "glicose", "glicemia", "sangue", "peso", "copo", "remedio",
                "medicamento", "medicamentos", "comprimido", "comprimidos", "dose", "doses",
                "horario", "horarios", "medicacao", "medicao", "medicoes", "registro", "registros",
                "valor", "dado", "dados", "esposa", "marido", "filho", "filha", "conta", "perfil",
                "saude", "beber", "tomar", "tomou", "comer", "um", "uma", "minha", "meu",
                "sua", "seu", "seus", "suas",
                "mamae", "papai", "mae", "pai", "familiar", "paciente", "senhor", "senhora"}
    for match in re.finditer(r"\b(?:do|da|de|para|pra|o|a)\s+(?:dona\s+|seu\s+)?([a-z]+)\b", _norm(text)):
        name = match.group(1)
        if name not in stopwords:
            return name
    return None


def mentions_other_person(text: str) -> bool:
    return bool(_OTHER_HINT.search(_norm(text)) or _explicit_person_name(text))


def _name_tokens(name: str) -> set:
    return {t for t in _norm(name).split() if len(t) >= 3}


def _matches_name(text_norm: str, name: str) -> bool:
    """Nome do familiar aparece na fala (tokens significativos)."""
    tokens = _name_tokens(name)
    if not tokens:
        return False
    return any(re.search(r"\b" + re.escape(t) + r"\b", text_norm) for t in tokens)


async def list_active_relations(conn: Any, actor_id: str) -> List[Any]:
    """Vínculos ativos onde o ator é cuidador: pacientes que ele acompanha."""
    return await conn.fetch(
        """SELECT b.patient_id, p.full_name, b.caregiver_role
           FROM family_bindings b JOIN profiles p ON p.id = b.patient_id
           WHERE b.caregiver_id = $1 AND b.status = 'ACTIVE'
           ORDER BY p.full_name""", actor_id)



async def resolve_family_target(conn: Any, context: OperationContext,
                                text: str) -> OperationResult:
    """
    Resolve o paciente-alvo a partir da fala, dos vínculos ativos do ator.

    Retorna:
    - success       : alvo único (ou nomeado sem ambiguidade); data.carrega
                      patient_id, full_name, role.
    - clarification : vários candidatos -> pede a escolha.
    - missing_data  : nenhum vínculo ativo -> orienta pelo app.
    - unavailable   : indisponibilidade do banco.
    """
    try:
        rows = await list_active_relations(conn, context.actor_id)
    except Exception:
        return OperationResult.unavailable(
            "Agora não consegui verificar seus vínculos familiares. "
            "Tente novamente em instantes.")

    if not rows:
        return OperationResult.missing_data(
            "Você ainda não acompanha ninguém pelo WhatsApp. No aplicativo "
            "Braga Saúde, convide um familiar em 'Vínculos de cuidado'; "
            "depois eu consigo consultar os dados dele aqui.")

    norm = _norm(text)
    # Tenta casar pelo nome primeiro ("como está a Dona Maria?").
    if norm:
        full_matches = [r for r in rows if len(_norm(r['full_name']).split()) > 1
                        and re.search(r"\b" + re.escape(_norm(r['full_name'])) + r"\b", norm)]
        by_name = full_matches or [r for r in rows if _matches_name(norm, r["full_name"])]
        if len(by_name) == 1:
            return _target_result(by_name[0])
        if len(by_name) > 1:
            return _choose_between(by_name)

    if _explicit_person_name(text):
        return OperationResult.clarification(
            "Não encontrei esse nome nos seus vínculos ativos. "
            "Qual familiar você quer consultar?", data={"candidates": [
                {"patient_id": str(r["patient_id"]), "full_name": r["full_name"]} for r in rows]})

    # caregiver_role representa permissão; caregiver_relation descreve o ator,
    # não o parentesco do paciente. Nenhum deles prova quem é "mamãe"/"pai".
    # Mesmo um único vínculo não autoriza substituir o sujeito mencionado.
    if _OTHER_HINT.search(re.sub(r"\b(?:familiar|paciente)\b", "", norm)):
        return OperationResult.clarification(
            "Preciso confirmar de quem você está falando. Qual é o nome do familiar?",
            data={"candidates": [
                {"patient_id": str(r["patient_id"]), "full_name": r["full_name"]} for r in rows]})

    # Sem nome: vínculo único resolve; vários pedem.
    if len(rows) == 1:
        return _target_result(rows[0])
    return _choose_between(rows)


def _target_result(row: Any) -> OperationResult:
    return OperationResult.success(
        f"Alvo: {first_name(row['full_name'])}.",
        data={"patient_id": str(row["patient_id"]),
              "full_name": row["full_name"],
              "role": row["caregiver_role"]})


def _choose_between(rows: List[Any]) -> OperationResult:
    labels = choice_labels(rows)
    lista = "\n".join(f"• {label}" for label in labels)
    return OperationResult.clarification(
        "Qual destas pessoas você quer consultar?\n\n"
        + lista +
        "\n\nMe diga o nome ou o número da opção, ou pergunte direto, tipo "
        "'qual a pressão da Maria?'.",
        data={"candidates": [
            {"patient_id": str(r["patient_id"]), "full_name": r["full_name"], "display_name": label}
            for r, label in zip(rows, labels)]})


def target_context(context: OperationContext, target: dict) -> OperationContext:
    """
    Novo contexto com o patient_id do familiar alvo (actor continua o mesmo).

    O vínculo NÃO é cacheado: a permissão é revalidada em cada operação
    (passo 4.2, validate_actor).
    """
    return context.model_copy(update={"patient_id": target["patient_id"]})


async def resolve_target_context(conn: Any, context: OperationContext,
                                 text: str) -> Tuple[Optional[OperationContext], Optional[OperationResult]]:
    """
    Atalho: resolve o alvo e devolve (novo_contexto, None) ou
    (None, resultado_do_erro). Autocuidado (sem menção a outra pessoa) devolve
    o contexto original intacto.
    """
    if not mentions_other_person(text):
        return context, None
    result = await resolve_family_target(conn, context, text)
    if not result.is_success or not result.data:
        return None, result
    return target_context(context, result.data), None
