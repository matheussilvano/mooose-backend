# Mooose Backend

## Referral system (Convide 1 amigo → ganhe 2 créditos)

### Fluxo
1. O front chama `GET /me/referral` para obter o `referral_code` e o link de convite.
2. No cadastro, o front envia `ref` e (opcionalmente) `device_fingerprint` em `POST /auth/register`.
3. A confirmação de créditos acontece quando o indicado estiver com e-mail verificado e tiver ao menos 1 correção.
   - O backend tenta ativar automaticamente após a primeira correção.
   - O front pode chamar `POST /referrals/activate` para forçar a checagem.

### Endpoints
- `GET /me/referral` (auth)
  - Resposta:
    ```json
    {
      "referral_code": "ABC123XYZ",
      "referral_link": "https://mooose.com.br/register?ref=ABC123XYZ",
      "reward_per_referral": 2,
      "stats": {
        "pending": 3,
        "confirmed": 5,
        "total_earned_credits": 10
      }
    }
    ```

- `POST /auth/register`
  - Body (exemplo):
    ```json
    {
      "email": "user@exemplo.com",
      "password": "senha",
      "ref": "ABC123XYZ",
      "device_fingerprint": "fp-123"
    }
    ```

- `POST /referrals/activate` (auth)
  - Resposta:
    ```json
    { "credited": true, "credits_added": 2 }
    ```

### Anti-fraude
- Sem auto-indicacao.
- Rate limit por IP no cadastro e ativacao (5/min).
- Usa `signup_ip` e `device_fingerprint` (se enviado).
- Regra simples: nao credita se `signup_ip` do indicado for igual ao do indicante.

### Configuracao
- `FRONTEND_URL` (default: https://mooose.com.br)
- `REFERRAL_REWARD_CREDITS` (default: 2)
- `REFERRAL_CODE_LENGTH` (default: 10; entre 8 e 12)

### Migrations
- Arquivo: `migrations/2026_01_27_referrals.sql`
- Para SQLite, recrie o banco local ou aplique as alteracoes manualmente.
