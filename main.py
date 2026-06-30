"""
Webhook de Rastreabilidade Automática
======================================
Recebe a notificação do Omie quando um Pedido de Venda muda de etapa.
Quando a etapa nova é "50" (Faturar), busca lote/validade no Neon
(tblotematriz) e grava automaticamente no pedido via AlterarPedidoVenda,
ANTES de alguém clicar em faturar.

Deploy: Render (mesmo padrão do projeto FRI Matriz -> ATIVA).
"""

import os
from datetime import date, datetime

from fastapi import FastAPI, Request

from utils.api_omie import consultar_pedido, consultar_produto, alterar_pedido_rastreabilidade
from utils.neon_select import buscar_lote_validade

app = FastAPI()

ETAPA_FATURAR = "50"

# guarda em memória os pedidos já processados nesta execução,
# pra não duplicar trabalho se o Omie reenviar o mesmo webhook
_pedidos_processados = set()


@app.get("/")
def health():
    return {"status": "ok"}


def _calcular_fabricacao_validade(validade_raw: str):
    """Mesma lógica do app Streamlit: aceita MM/YYYY ou DD/MM/YYYY,
    e calcula fabricação = validade - 3 anos."""
    if not validade_raw or validade_raw in ("S/V", "-"):
        return "", ""

    try:
        if len(validade_raw.split("/")) == 2:
            mes, ano = validade_raw.split("/")
            mes = int(mes)
            ano = int(ano) + 2000 if int(ano) < 100 else int(ano)
            validade_dt = date(ano, mes, 1)
        else:
            validade_dt = datetime.strptime(validade_raw, "%d/%m/%Y").date()

        fabricacao_dt = date(validade_dt.year - 3, validade_dt.month, validade_dt.day)

        return fabricacao_dt.strftime("%d/%m/%Y"), validade_dt.strftime("%d/%m/%Y")
    except Exception as e:
        print(f"⚠️ Erro ao converter validade '{validade_raw}': {e}")
        return "", ""


def processar_pedido(numero_pedido):
    if numero_pedido in _pedidos_processados:
        print(f"↪️ Pedido {numero_pedido} já processado nesta execução, ignorando.")
        return

    dados = consultar_pedido(numero_pedido)

    cabecalho = dados.get("pedido_venda_produto", {}).get("cabecalho", {})
    etapa = cabecalho.get("etapa", "")
    codigo_pedido = cabecalho.get("codigo_pedido")

    if etapa != ETAPA_FATURAR:
        print(f"↪️ Pedido {numero_pedido} está na etapa {etapa}, não é a etapa de faturar. Ignorando.")
        return

    itens = dados.get("pedido_venda_produto", {}).get("det", [])
    det_atualizado = []
    algum_lote_encontrado = False

    for item in itens:
        produto = item.get("produto", {})
        codigo_produto = produto.get("codigo_produto")
        codigo_item_integracao = item.get("ide", {}).get("codigo_item_integracao")

        _descricao, sku = consultar_produto(codigo_produto)

        lote, validade_raw = (None, None)
        if sku:
            lote, validade_raw = buscar_lote_validade(sku)

        if not lote:
            print(f"⚠️ SKU {sku} (produto {codigo_produto}) sem lote cadastrado no Neon. Pulando rastreabilidade deste item.")
            continue

        fabricacao_str, validade_str = _calcular_fabricacao_validade(validade_raw)

        det_atualizado.append({
            "ide": {"codigo_item_integracao": codigo_item_integracao},
            "rastreabilidade": {
                "numeroLote": lote,
                "dataFabricacaoLote": fabricacao_str,
                "dataValidadeLote": validade_str,
                "qtdeProdutoLote": produto.get("quantidade"),
            },
        })
        algum_lote_encontrado = True

    if not algum_lote_encontrado:
        print(f"⚠️ Nenhum lote encontrado para nenhum item do pedido {numero_pedido}. Nada gravado.")
        return

    resultado = alterar_pedido_rastreabilidade(codigo_pedido, det_atualizado)
    print(f"✅ Pedido {numero_pedido} atualizado: {resultado}")

    _pedidos_processados.add(numero_pedido)


@app.get("/webhook/etapa-pedido")
def webhook_etapa_pedido_check():
    """Responde ao teste de validação que o Omie faz ao salvar o webhook."""
    return {"status": "ok"}


@app.post("/webhook/etapa-pedido")
async def webhook_etapa_pedido(request: Request):
    """
    Formato real confirmado pelo webhook do Omie em produção:

    {
      "messageId": "...",
      "topic": "VendaProduto.EtapaAlterada",
      "event": {
        "numeroPedido": "34552",
        "etapa": "50",
        "etapaDescr": "Faturar",
        "idPedido": 9190977970,
        "codIntPedido": "27772",
        ...
      },
      "author": {...},
      "appKey": "...",
      ...
    }
    """
    payload = await request.json()
    print("===== WEBHOOK RECEBIDO =====")
    print(payload)
    print("=============================")

    topico = payload.get("topic")
    if topico != "VendaProduto.EtapaAlterada":
        print(f"↪️ Tópico '{topico}' não é de interesse. Ignorando.")
        return {"status": "ignorado", "motivo": "topico não monitorado"}

    evento = payload.get("event", {})
    numero_pedido = evento.get("numeroPedido")
    etapa = evento.get("etapa")

    if not numero_pedido:
        print("❌ Não foi possível identificar o numeroPedido no payload.")
        return {"status": "ignorado", "motivo": "numeroPedido não encontrado"}

    if etapa != ETAPA_FATURAR:
        print(f"↪️ Pedido {numero_pedido} mudou pra etapa {etapa} ({evento.get('etapaDescr')}), não é a etapa de faturar ({ETAPA_FATURAR}). Ignorando.")
        return {"status": "ignorado", "motivo": f"etapa {etapa} não monitorada"}

    try:
        processar_pedido(numero_pedido)
        return {"status": "ok"}
    except Exception as e:
        print(f"❌ Erro ao processar pedido {numero_pedido}: {e}")
        return {"status": "erro", "mensagem": str(e)}