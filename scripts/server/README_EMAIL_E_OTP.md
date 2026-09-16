# E-mail e proteção de OTP

O backend envia OTPs pelo Gmail SMTP. Defina as variáveis abaixo no ambiente do
serviço `braga-gateway` (não no código e nunca no Git):

```ini
GMAIL_SMTP_HOST=smtp.gmail.com
GMAIL_SMTP_PORT=465
GMAIL_SMTP_USERNAME=seu-remetente@gmail.com
GMAIL_SMTP_APP_PASSWORD=sua_senha_de_app_de_16_caracteres
GMAIL_FROM_EMAIL=seu-remetente@gmail.com
GMAIL_FROM_NAME=Braga Saúde
```

Para obter a senha, ative a verificação em duas etapas da conta Google e crie
uma senha de app exclusiva para o backend. Não use a senha normal da conta.
Revogue a senha de app se ela for exposta ou se o servidor deixar de usá-la.

## Rate limiting atual

O limitador está na memória do processo FastAPI e devolve `429` com o cabeçalho
`Retry-After` antes de tocar no PostgreSQL:

| Rota | Chave | Limite |
| --- | --- | --- |
| `/api/auth/check-account` | IP | 5/minuto |
| `/api/auth/otp/send` | identificador | 3/15 minutos |
| `/api/auth/otp/send` | IP | 10/15 minutos |
| `/api/auth/otp/verify` | identificador e IP | 5/minuto |

Para uma implantação com mais de uma réplica do FastAPI, os contadores devem
migrar para Redis; memória não é compartilhada entre processos ou servidores.
Use `TRUST_PROXY_HEADERS=true` somente se a API estiver atrás de um proxy reverso
controlado que sobrescreva `X-Forwarded-For`.
