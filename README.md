# Webhook de Rastreabilidade Automática (Pedido de Venda)

Grava lote/validade no pedido automaticamente quando ele entra na etapa
**50 (Faturar)**, usando a base já confiável do Neon (`tblotematriz`).
Elimina o passo de abrir o Streamlit e digitar manualmente antes de
alguém clicar em faturar.

## Como funciona

1. Omie dispara um webhook quando QUALQUER pedido muda de etapa.
2. O endpoint `/webhook/etapa-pedido` recebe a notificação.
3. Se a etapa nova for `50`, consulta o pedido completo, resolve o SKU
   de cada item, busca lote/validade no Neon e chama `AlterarPedidoVenda`
   gravando a rastreabilidade.
4. Como você confirmou que ninguém fatura automaticamente ao entrar na
   etapa 50 (alguém ainda clica depois), não há risco de corrida: o
   webhook tem tempo de sobra pra gravar antes do clique em faturar.

## Passo 1 — Configurar o webhook no Omie

1. Acesse o [Portal do Desenvolvedor Omie](https://developer.omie.com.br/) → **Aplicativos**.
2. Abra o seu aplicativo (o mesmo que você já usa pras outras integrações).
3. Clique em **⚙️ Adicionar novo webhook**.
4. Ative o evento de **mudança de etapa de Pedido de Venda** (na lista de
   eventos disponíveis — o nome exato pode variar, procure por algo como
   "VendaProduto" + etapa).
5. Antes de apontar pra produção, teste com um RequestBin
   (https://requestbin.com) pra ver o payload real que o Omie manda —
   os nomes de campo no `main.py` (`numero_pedido`, `nNumPedido`, etc.)
   são um palpite baseado no padrão dos outros webhooks Omie e
   **precisam ser confirmados/ajustados** com o payload real antes de ir
   pra produção.
6. Depois de confirmado, aponte o webhook pra:
   `https://SEU-SERVICO.onrender.com/webhook/etapa-pedido`

## Passo 2 — Variáveis de ambiente (Render)

| Variável       | Descrição                          |
|----------------|-------------------------------------|
| `APP_KEY`      | App Key da Omie                     |
| `APP_SECRET`   | App Secret da Omie                  |
| `NEON_DB_URL`  | Connection string do Neon           |

⚠️ Use uma App Key/Secret dedicada (ou pelo menos regenerada), igual
você já fez no projeto FRI Matriz — nunca reaproveite chaves expostas em
conversas anteriores.

## Passo 3 — Deploy no Render

Mesmo fluxo do projeto FRI Matriz → ATIVA:

1. Suba esta pasta como repositório no GitHub (org `Lenvie-Parfums`).
2. No Render: **New → Web Service** → conecte o repo.
3. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Configure as 3 variáveis de ambiente acima.
5. Deploy.

## Passo 4 — Testar

1. Pegue um pedido de teste, garanta que o SKU dos itens tem lote/validade
   cadastrado no Neon.
2. Mova o pedido manualmente pra etapa 50 no Omie.
3. Acompanhe os logs no Render — deve aparecer "✅ Pedido X atualizado".
4. Confira no Omie se o campo de rastreabilidade do item foi preenchido.

## Pontos de atenção

- **Idempotência**: o serviço guarda em memória os pedidos já processados
  na execução atual, pra não duplicar se o Omie reenviar o webhook. Como
  é em memória, reinicia a cada deploy/restart do Render — não é um
  problema grave aqui porque `AlterarPedidoVenda` é idempotente (só
  sobrescreve o mesmo valor), mas se quiser robustez total dá pra trocar
  por uma tabela no Neon marcando `codigo_pedido` processado.
- **SKU sem lote no Neon**: o item é pulado e fica registrado no log —
  vale criar um alerta (Slack, igual você já fez no projeto de ruptura)
  pra esses casos, senão a NF pode sair sem rastreabilidade naquele item
  sem ninguém perceber.
- **Rate limit Omie**: o `_chamada_omie` já tem o retry automático que
  você usa nos outros projetos, pra lidar com "Consumo redundante".
