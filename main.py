"""
Webhook de Rastreabilidade Automática
======================================
Dois fluxos cobertos:

1. Pedido de Venda (Matriz e 003)
   Tópico : VendaProduto.EtapaAlterada (case-insensitive)
   Gatilho: etapa == "20" (Separação)
   Ação   : AlterarPedidoVenda com rastreabilidade + especie/marca frete

2. Remessa (Matriz e 003)
   Tópico : RemessaProduto.Alterada (case-insensitive)
   Gatilho: criação de qualquer remessa
   Ação   : AlterarRemessa com rastreabilidade + especie/marca frete

Deploy: Render — mesmo padrão do projeto FRI Matriz -> ATIVA.
"""

import json
from datetime import date, datetime

from fastapi import FastAPI, Request

from utils.api_omie import (
    consultar_pedido,
    consultar_produto,
    alterar_pedido_rastreabilidade,
    consultar_remessa,
    alterar_remessa_rastreabilidade,
)
from utils.neon_select import buscar_lote_validade

app = FastAPI()

ETAPA_SEPARACAO = "20"

_pedidos_processados: set = set()
_remessas_processadas: set = set()


# ── UTILITÁRIOS ────────────────────────────────────────────────────────────────

def _calcular_fabricacao_validade(validade_raw: str):
    """Aceita MM/YYYY ou DD/MM/YYYY. Retorna (fabricacao, validade) no formato DD/MM/YYYY."""
    if not validade_raw or validade_raw.strip() in ("S/V", "-", ""):
        return "", ""
    try:
        partes = validade_raw.strip().split("/")
        if len(partes) == 2:
            mes, ano = int(partes[0]), int(partes[1])
            if ano < 100:
                ano += 2000
            validade_dt = date(ano, mes, 1)
        else:
            validade_dt = datetime.strptime(validade_raw.strip(), "%d/%m/%Y").date()

        fabricacao_dt = date(validade_dt.year - 3, validade_dt.month, validade_dt.day)
        return fabricacao_dt.strftime("%d/%m/%Y"), validade_dt.strftime("%d/%m/%Y")
    except Exception as e:
        print(f"⚠️ Erro ao converter validade '{validade_raw}': {e}")
        return "", ""


def _topico_match(payload: dict, esperado: str) -> bool:
    """Compara tópico do payload com o esperado de forma case-insensitive e segura."""
    topico = payload.get("topic") or ""
    return topico.lower() == esperado.lower()


# ── HEALTH CHECK ───────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok"}


# ── PEDIDO DE VENDA ────────────────────────────────────────────────────────────

def _processar_pedido(numero_pedido: str, app_key_origem: str):
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
    algum_lote = False

    for idx, item in enumerate(itens, start=1):
        produto = item.get("produto", {})
        codigo_produto = produto.get("codigo_produto")
        codigo_item_integracao = item.get("ide", {}).get("codigo_item_integracao") or str(idx)

        _desc, sku = consultar_produto(codigo_produto, app_key_origem)

        lote, validade_raw = None, None
        if sku:
            lote, validade_raw = buscar_lote_validade(sku)

        if not lote:
            print(f"⚠️ SKU {sku} (produto {codigo_produto}) sem lote no Neon. Pulando item.")
            continue

        fab, val = _calcular_fabricacao_validade(validade_raw)

        rastreabilidade = {"numeroLote": lote, "qtdeProdutoLote": produto.get("quantidade")}
        if fab:
            rastreabilidade["dataFabricacaoLote"] = fab
        if val:
            rastreabilidade["dataValidadeLote"] = val

        det_atualizado.append({
            "ide": {"codigo_item_integracao": codigo_item_integracao},
            "rastreabilidade": rastreabilidade,
        })
        algum_lote = True

    if not algum_lote:
        print(f"⚠️ Nenhum lote encontrado para o pedido {numero_pedido}. Nada gravado.")
        return

    resultado = alterar_pedido_rastreabilidade(codigo_pedido, det_atualizado, app_key_origem)
    print(f"✅ Pedido {numero_pedido} atualizado: {resultado}")
    _pedidos_processados.add(numero_pedido)


@app.get("/webhook/etapa-pedido")
def webhook_etapa_pedido_check():
    return {"status": "ok"}


@app.post("/webhook/etapa-pedido")
async def webhook_etapa_pedido(request: Request):
    payload = await request.json()
    print("===== WEBHOOK PEDIDO RECEBIDO =====")
    print(payload)
    print("===================================")

    if not _topico_match(payload, "VendaProduto.EtapaAlterada"):
        topico = payload.get("topic", "")
        print(f"↪️ Tópico '{topico}' ignorado.")
        return {"status": "ignorado", "motivo": "topico não monitorado"}

    evento = payload.get("event", {})
    numero_pedido = evento.get("numeroPedido")
    etapa = evento.get("etapa")
    app_key_origem = payload.get("appKey", "")

    if not numero_pedido:
        print("❌ numeroPedido não encontrado no payload.")
        return {"status": "ignorado", "motivo": "numeroPedido não encontrado"}

    if etapa != ETAPA_SEPARACAO:
        print(f"↪️ Pedido {numero_pedido} foi pra etapa {etapa}, não é separação. Ignorando.")
        return {"status": "ignorado", "motivo": f"etapa {etapa} não monitorada"}

    try:
        _processar_pedido(numero_pedido, app_key_origem)
        return {"status": "ok"}
    except Exception as e:
        print(f"❌ Erro ao processar pedido {numero_pedido}: {e}")
        return {"status": "erro", "mensagem": str(e)}


# ── REMESSA ────────────────────────────────────────────────────────────────────

def _processar_remessa(cod_remessa: int, app_key_origem: str):
    if cod_remessa in _remessas_processadas:
        print(f"↪️ Remessa {cod_remessa} já processada nesta execução, ignorando.")
        return

    dados = consultar_remessa(cod_remessa, app_key_origem)

    # log completo pra confirmar estrutura real do retorno do Omie
    print(f"===== DADOS DA REMESSA {cod_remessa} =====")
    print(json.dumps(dados, indent=2, ensure_ascii=False, default=str))
    print("==========================================")

    cabec = dados.get("cabec", {})
    cod_cliente = cabec.get("nCodCli")
    numero_remessa = cabec.get("cNumeroRemessa", str(cod_remessa))

    itens = dados.get("produtos", [])
    produtos_atualizados = []
    algum_lote = False

    for item in itens:
        codigo_produto = item.get("nCodProd")
        cod_item = item.get("nCodIt")
        quantidade = item.get("nQtde")
        val_unit = item.get("nValUnit")

        _desc, sku = consultar_produto(codigo_produto, app_key_origem)

        lote, validade_raw = None, None
        if sku:
            lote, validade_raw = buscar_lote_validade(sku)

        if not lote:
            print(f"⚠️ SKU {sku} (produto {codigo_produto}) sem lote no Neon. Incluindo item sem rastreabilidade.")
            produtos_atualizados.append({
                "nCodProd": codigo_produto,
                "nCodIt": cod_item,
                "nQtde": quantidade,
                "nValUnit": val_unit,
            })
            continue

        fab, val = _calcular_fabricacao_validade(validade_raw)

        rastreabilidade = {"numeroLote": lote, "qtdeProdutoLote": quantidade}
        if fab:
            rastreabilidade["dataFabricacaoLote"] = fab
        if val:
            rastreabilidade["dataValidadeLote"] = val

        produtos_atualizados.append({
            "nCodProd": codigo_produto,
            "nCodIt": cod_item,
            "nQtde": quantidade,
            "nValUnit": val_unit,
            "rastreabilidade": rastreabilidade,
        })
        algum_lote = True

    if not algum_lote:
        print(f"⚠️ Nenhum lote encontrado para a remessa {numero_remessa}. Nada gravado.")
        return

    resultado = alterar_remessa_rastreabilidade(cod_remessa, cod_cliente, produtos_atualizados, app_key_origem)
    print(f"✅ Remessa {numero_remessa} atualizada: {resultado}")
    _remessas_processadas.add(cod_remessa)


@app.get("/webhook/remessa-criada")
def webhook_remessa_check():
    return {"status": "ok"}


@app.post("/webhook/remessa-criada")
async def webhook_remessa_criada(request: Request):
    payload = await request.json()
    print("===== WEBHOOK REMESSA RECEBIDO =====")
    print(payload)
    print("=====================================")

    if not _topico_match(payload, "RemessaProduto.Alterada"):
        topico = payload.get("topic", "")
        print(f"↪️ Tópico '{topico}' ignorado.")
        return {"status": "ignorado", "motivo": "topico não monitorado"}

    evento = payload.get("event", {})
    app_key_origem = payload.get("appKey", "")
    cod_remessa = evento.get("idRemessa") or evento.get("nCodRem") or evento.get("codRem")

    if not cod_remessa:
        print("❌ Código da remessa não encontrado no payload.")
        return {"status": "ignorado", "motivo": "idRemessa não encontrado"}

    try:
        _processar_remessa(int(cod_remessa), app_key_origem)
        return {"status": "ok"}
    except Exception as e:
        print(f"❌ Erro ao processar remessa {cod_remessa}: {e}")
        return {"status": "erro", "mensagem": str(e)}