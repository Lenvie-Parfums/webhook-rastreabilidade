"""
Webhook de Rastreabilidade Automática
======================================
Recebe a notificação do Omie quando um Pedido de Venda muda de etapa.
Quando a etapa nova é "20" (Separação), busca lote/validade no Neon
(tblotematriz), grava automaticamente no pedido via AlterarPedidoVenda
e preenche especie_volumes/marca_volumes no frete.

Deploy: Render (mesmo padrão do projeto FRI Matriz -> ATIVA).
"""

import os
from datetime import date, datetime

from fastapi import FastAPI, Request

from utils.api_omie import (
    consultar_pedido, consultar_produto, alterar_pedido_rastreabilidade,
    consultar_remessa, alterar_remessa_rastreabilidade,
)
from utils.neon_select import buscar_lote_validade

app = FastAPI()

ETAPA_SEPARACAO = "20"

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


def processar_pedido(numero_pedido, app_key_origem: str):
    if numero_pedido in _pedidos_processados:
        print(f"↪️ Pedido {numero_pedido} já processado nesta execução, ignorando.")
        return

    dados = consultar_pedido(numero_pedido, app_key_origem)

    cabecalho = dados.get("pedido_venda_produto", {}).get("cabecalho", {})
    etapa = cabecalho.get("etapa", "")
    codigo_pedido = cabecalho.get("codigo_pedido")

    if etapa != ETAPA_SEPARACAO:
        print(f"↪️ Pedido {numero_pedido} está na etapa {etapa}, não é separação. Ignorando.")
        return

    itens = dados.get("pedido_venda_produto", {}).get("det", [])
    det_atualizado = []
    algum_lote_encontrado = False

    for idx, item in enumerate(itens, start=1):
        produto = item.get("produto", {})
        codigo_produto = produto.get("codigo_produto")

        codigo_item_integracao = item.get("ide", {}).get("codigo_item_integracao") or str(idx)

        _descricao, sku = consultar_produto(codigo_produto, app_key_origem)

        lote, validade_raw = (None, None)
        if sku:
            lote, validade_raw = buscar_lote_validade(sku)

        if not lote:
            print(f"⚠️ SKU {sku} (produto {codigo_produto}) sem lote cadastrado no Neon. Pulando rastreabilidade deste item.")
            continue

        fabricacao_str, validade_str = _calcular_fabricacao_validade(validade_raw)

        rastreabilidade = {
            "numeroLote": lote,
            "qtdeProdutoLote": produto.get("quantidade"),
        }
        if fabricacao_str:
            rastreabilidade["dataFabricacaoLote"] = fabricacao_str
        if validade_str:
            rastreabilidade["dataValidadeLote"] = validade_str

        det_atualizado.append({
            "ide": {"codigo_item_integracao": codigo_item_integracao},
            "rastreabilidade": rastreabilidade,
        })
        algum_lote_encontrado = True

    if not algum_lote_encontrado:
        print(f"⚠️ Nenhum lote encontrado para nenhum item do pedido {numero_pedido}. Nada gravado.")
        return

    resultado = alterar_pedido_rastreabilidade(codigo_pedido, det_atualizado, app_key_origem)
    print(f"✅ Pedido {numero_pedido} atualizado: {resultado}")

    _pedidos_processados.add(numero_pedido)


_remessas_processadas = set()


def processar_remessa(cod_remessa: int, app_key_origem: str):
    if cod_remessa in _remessas_processadas:
        print(f"↪️ Remessa {cod_remessa} já processada nesta execução, ignorando.")
        return

    dados = consultar_remessa(cod_remessa, app_key_origem)

    cabec = dados.get("cabec", {})
    cod_cliente = cabec.get("nCodCli")
    numero_remessa = cabec.get("cNumeroRemessa", cod_remessa)

    itens = dados.get("produtos", [])
    produtos_atualizados = []
    algum_lote_encontrado = False

    for item in itens:
        codigo_produto = item.get("nCodProd")
        cod_item       = item.get("nCodIt")
        quantidade     = item.get("nQtde")
        val_unit       = item.get("nValUnit")

        _descricao, sku = consultar_produto(codigo_produto, app_key_origem)

        lote, validade_raw = (None, None)
        if sku:
            lote, validade_raw = buscar_lote_validade(sku)

        if not lote:
            print(f"⚠️ SKU {sku} (produto {codigo_produto}) sem lote no Neon. Pulando item.")
            # inclui o item sem rastreabilidade pra não remover do payload
            produtos_atualizados.append({
                "nCodProd": codigo_produto,
                "nCodIt":   cod_item,
                "nQtde":    quantidade,
                "nValUnit": val_unit,
            })
            continue

        fabricacao_str, validade_str = _calcular_fabricacao_validade(validade_raw)

        rastreabilidade = {
            "numeroLote":      lote,
            "qtdeProdutoLote": quantidade,
        }
        if fabricacao_str:
            rastreabilidade["dataFabricacaoLote"] = fabricacao_str
        if validade_str:
            rastreabilidade["dataValidadeLote"] = validade_str

        produtos_atualizados.append({
            "nCodProd":        codigo_produto,
            "nCodIt":          cod_item,
            "nQtde":           quantidade,
            "nValUnit":        val_unit,
            "rastreabilidade": rastreabilidade,
        })
        algum_lote_encontrado = True

    if not algum_lote_encontrado:
        print(f"⚠️ Nenhum lote encontrado pra nenhum item da remessa {numero_remessa}. Nada gravado.")
        return

    resultado = alterar_remessa_rastreabilidade(cod_remessa, cod_cliente, produtos_atualizados, app_key_origem)
    print(f"✅ Remessa {numero_remessa} atualizada: {resultado}")

    _remessas_processadas.add(cod_remessa)


@app.get("/webhook/remessa-criada")
def webhook_remessa_check():
    """Responde ao teste de validação que o Omie faz ao salvar o webhook."""
    return {"status": "ok"}


@app.post("/webhook/remessa-criada")
async def webhook_remessa_criada(request: Request):
    """
    Escuta o evento RemessaProduto.Incluida do Omie (Matriz e 003).
    Payload real confirmado via teste:

    {
      "topic": "RemessaProduto.Incluida",
      "event": {
        "idRemessa": 4081384374,
        "numeroRemessa": "2952",
        "idCliente": 3554834118,
        "etapa": "10",
        "dataInclusao": "02/07/2026",
        ...
      },
      "appKey": "...",
      ...
    }
    """
    payload = await request.json()
    print("===== WEBHOOK REMESSA RECEBIDO =====")
    print(payload)
    print("=====================================")

    topico = payload.get("topic")
    if topico != "RemessaProduto.Incluida":
        print(f"↪️ Tópico '{topico}' não é RemessaProduto.Incluida. Ignorando.")
        return {"status": "ignorado", "motivo": "topico não monitorado"}

    evento = payload.get("event", {})
    app_key_origem = payload.get("appKey", "")

    # campo confirmado via teste real: 'idRemessa'
    cod_remessa = evento.get("idRemessa") or evento.get("nCodRem") or evento.get("codRem")

    if not cod_remessa:
        print("❌ Não foi possível identificar nCodRem no payload.")
        return {"status": "ignorado", "motivo": "nCodRem não encontrado"}

    try:
        processar_remessa(int(cod_remessa), app_key_origem)
        return {"status": "ok"}
    except Exception as e:
        print(f"❌ Erro ao processar remessa {cod_remessa}: {e}")
        return {"status": "erro", "mensagem": str(e)}
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
        "etapa": "20",
        "etapaDescr": "Separação",
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
    app_key_origem = payload.get("appKey", "")

    if not numero_pedido:
        print("❌ Não foi possível identificar o numeroPedido no payload.")
        return {"status": "ignorado", "motivo": "numeroPedido não encontrado"}

    if etapa != ETAPA_SEPARACAO:
        print(f"↪️ Pedido {numero_pedido} mudou pra etapa {etapa} ({evento.get('etapaDescr')}), não é separação ({ETAPA_SEPARACAO}). Ignorando.")
        return {"status": "ignorado", "motivo": f"etapa {etapa} não monitorada"}

    try:
        processar_pedido(numero_pedido, app_key_origem)
        return {"status": "ok"}
    except Exception as e:
        print(f"❌ Erro ao processar pedido {numero_pedido}: {e}")
        return {"status": "erro", "mensagem": str(e)}