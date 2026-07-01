# Webhook de Rastreabilidade Automática (Pedido de Venda)

Grava lote/validade e campos de frete (CAIXAS / LENVIE) automaticamente
quando o pedido entra na etapa **20 (Separação)**, usando a base do Neon
(`tblotematriz`). Elimina o passo de abrir qualquer app ou digitar
manualmente antes de faturar.

## Como funciona

1. Omie dispara um webhook quando QUALQUER pedido muda de etapa.
2. O endpoint `/webhook/etapa-pedido` recebe a notificação.
3. Se a etapa nova for `20` (Separação), consulta o pedido completo,
   resolve o SKU de cada item, busca lote/validade no Neon e chama
   `AlterarPedidoVenda` gravando:
   - `rastreabilidade` (lote, fabricação, validade) em cada item
   - `frete.especie_volumes = "CAIXAS"` e `frete.marca_volumes = "LENVIE"`
4. Como o faturamento ainda exige clique manual, o webhook tem tempo
   de sobra pra gravar tudo antes de alguém clicar em faturar.

## Variáveis de ambiente (Render)

| Variável      | Descrição                |
|---------------|--------------------------|
| `APP_KEY`     | App Key da Omie (Matriz) |
| `APP_SECRET`  | App Secret da Omie       |
| `NEON_DB_URL` | Connection string do Neon |

## Deploy no Render

Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

## Pontos de atenção

- **Idempotência**: pedidos já processados ficam em memória e são ignorados
  se o Omie reenviar o webhook. Reinicia a cada deploy/restart do Render,
  mas `AlterarPedidoVenda` é idempotente então não há problema.
- **SKU sem lote no Neon**: o item é pulado e fica no log — considere
  criar um alerta Slack pra não passar batido.
- **Rate limit / timeout Omie**: o wrapper `_chamada_omie` já tem retry
  automático com espera de 10s entre tentativas (até 4x).
- **Datas inválidas**: se a validade no Neon estiver vazia ou em formato
  inesperado, o lote é gravado sem as datas — evita rejeição da SEFAZ
  pelo erro 1839 (`dVal: - -`).