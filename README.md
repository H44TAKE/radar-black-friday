# Radar Black Friday 2026

Monitor pessoal de preços que roda sozinho na nuvem (GitHub Actions, grátis) e avisa **no seu celular** (Telegram) quando acha oferta que vale a pena. Não compra nada, não usa seus dados pessoais.

## Como funciona

```
GitHub Actions (a cada 5 min) ──► python -m radar run
   ├─ lojas: Terabyte · KaBuM! · Pichau · Mercado Livre · AliExpress* · Shopee*     (*via API oficial de afiliados)
   ├─ comunidades: canais públicos do Telegram · grupos onde seu bot está · Instagram (API oficial, perfis comerciais)
   ├─ classificação rígida (ex.: water cooler só com RGB ≠ water cooler com tela LCD personalizável)
   ├─ histórico de preços (SQLite) → detecta falsa promoção, aumento pré-Black Friday, menor preço já visto
   ├─ verificação na página do produto antes de avisar (preço, estoque, novo/usado, vendedor)
   └─ Telegram Bot ──► seu celular      +  painel (GitHub Pages) com status, preços e histórico
```

Frequências (em `config/watchlist.yml`): produtos prioritários a cada 10 min, categorias a cada 30 min, comunidades a cada 5 min.
O GitHub não permite agendar em menos de 5 minutos e pode atrasar alguns minutos em horário de pico — é a limitação real da hospedagem gratuita.

## Configuração (uma vez só, ~10 minutos)

1. **Repositório**: crie um repositório **público** vazio no GitHub (público = minutos de Actions ilimitados) e suba este projeto.
2. **Bot do Telegram**: no Telegram, fale com `@BotFather` → `/newbot` → guarde o **token**. Mande qualquer mensagem para o seu bot e abra
   `https://api.telegram.org/bot<TOKEN>/getUpdates` para ver o seu **chat id** (`"chat":{"id": 123456789`).
3. **Secrets** (Settings → Secrets and variables → Actions → New repository secret):
   `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID` (obrigatórios). Opcionais: `NTFY_TOPIC`, `HC_PING_URL`,
   `ML_CLIENT_ID`/`ML_CLIENT_SECRET`, `ALI_APP_KEY`/`ALI_APP_SECRET`, `SHOPEE_APP_ID`/`SHOPEE_SECRET`, `IG_USER_ID`/`IG_ACCESS_TOKEN`.
   Nenhuma credencial fica no código.
4. **Painel**: Settings → Pages → Source: *Deploy from a branch* → branch `data` / `(root)`. O painel fica em `https://<usuario>.github.io/<repo>/`.
5. **Teste ao vivo**: Actions → **diagnostico** → *Run workflow*. Ele roda os testes, manda uma mensagem de teste pro celular e mostra o que cada loja/canal devolveu.
6. Actions → **monitor** → *Run workflow* (tier `all`) para o primeiro ciclo. Depois é automático.

## Como usar

| Quero… | Faça |
|---|---|
| Adicionar produto | `config/watchlist.yml` → `products:` (copie o bloco do Isengard; `match_all` = palavras que o título precisa ter) |
| Mudar limite de preço | `max_price:` do produto/categoria (Braços R$ 85, Water cooler LCD R$ 600) |
| Mais/menos sensibilidade | `min_drop_pct`, `resend_drop_pct`, `unverified_claim_min_pct` |
| Adicionar canal do Telegram | `telegram.channels` → `- handle: nome_do_canal` (canal **público**) |
| Grupo fechado seu | adicione o bot ao grupo e desligue *Group Privacy* no BotFather |
| Perfil do Instagram | `instagram.profiles: [usuario]` (precisa de `IG_*`; só contas Comercial/Criador públicas) |
| Saber se está vivo | painel (selo verde/amarelo/vermelho), mensagem diária às 9h, ou `HC_PING_URL` (healthchecks.io) |

## Regras de segurança do sistema

- Só notifica se o preço foi **lido ao vivo na página do produto**, está **em estoque**, é **novo** e (em marketplaces) o vendedor é confiável.
- Água com RGB não vale: a categoria LCD exige tela **e** evidência de personalização (ou modelo na lista `known_lcd_models`).
- Desconto sem histórico suficiente aparece como **"NÃO confirmado"** e tem limite diário.
- Links de comunidades só são abertos se o domínio estiver em `trusted_domains` (anti-golpe). Posts antigos (> 8h), links quebrados e esgotados são descartados.
- Loja bloqueou? O sistema **não tenta burlar**: marca a fonte como bloqueada, faz backoff e avisa na mensagem diária.
- Respeita `robots.txt`.

## Limitações (sem rodeios)

- Os seletores das lojas foram escritos a partir de páginas públicas e padrões documentados; o HTML ao vivo só pode ser validado na nuvem (workflow *diagnostico*). Se uma loja mudar o layout, a fonte aparece como falha — ajuste em `radar/collectors/`.
- IPs de datacenter (GitHub) podem ser bloqueados por lojas com proteção anti-bot (Cloudflare/Akamai). Alternativa legítima: rodar o mesmo workflow num *self-hosted runner* num aparelho seu (ex.: Raspberry Pi) com IP residencial.
- **AliExpress e Shopee**: sem raspagem; só via API de afiliados (cadastro gratuito). Sem credenciais ficam desativadas e as ofertas delas ainda chegam pelas comunidades.
- **Instagram**: a única via oficial é a Business Discovery API — só perfis Comercial/Criador públicos; **não lê Stories**, contas pessoais/privadas nem faz "link na bio". Alternativa: seguir os mesmos criadores no Telegram.
- **Telegram**: canais de terceiros só são lidos se tiverem visualização web pública; grupos fechados exigem o seu bot dentro.
- **Frete**: calculado só onde a fonte informa (ex.: frete grátis no Mercado Livre); nas demais lojas aparece "não calculado".
- **Cupons**: capturados quando divulgados em comunidades/loja; a validação real do cupom no carrinho não é automatizada.
- O painel e o histórico ficam num branch público do repositório (sem segredos).

## Desenvolvimento

```bash
pip install -r requirements.txt
python -m pytest -q                       # 29 testes de lógica
python -m radar --dry-run run --tier all  # imprime as mensagens em vez de enviar
```
