"""Socorro acessível em todos os canais, sem discagem automática."""
SAMU_URL = 'tel:192'
UPA_URL = 'https://www.google.com/maps/search/?api=1&query=UNIDADE+DE+PRONTO+ATENDIMENTO'


def emergency_reply():
    return (
        'Isso parece urgente. Acione o SAMU 192 agora.\n\n'
        f'Ligar 192: {SAMU_URL}\n'
        'Se não conseguir abrir o discador pelo link, disque 192.\n\n'
        f'Buscar unidade de pronto atendimento no Maps:\n{UPA_URL}\n\n'
        'Se puder, conte o que está sentindo e há quanto tempo, sem adiar o pedido de socorro.'
    )
