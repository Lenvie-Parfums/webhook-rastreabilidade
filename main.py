"""
Webhook de Rastreabilidade Automática
======================================
Preenche lote/validade + espécie/marca de volumes automaticamente, cobrindo
dois documentos e duas empresas (Matriz e Manifesto 003, roteadas por appKey).

FLUXO PEDIDO DE VENDA
  Tópico  : VendaProduto.EtapaAlterada (case-insensitive)
  Gatilhos: etapas 10, 20 e 50 (gravação progressiva)
  Regra   : cada etapa grava tudo que encontrar no Neon. Se a chamada ao Omie
            volta SEM erro técnico, o pedido é marcado como resolvido e as
            etapas seguintes saem na hora (sem tocar no Omie). Se a chamada
            falhar (rede/timeout/faultstring), a próxima etapa retenta.
            SKU que não existe no Neon é descartado — não bloqueia nem conta.

FLUXO REMESSA
  Tópico  : RemessaProduto.Alterada (case-insensitive)
  Gatilho : qualquer alteração da remessa (a criação vem vazia; os produtos
            chegam numa alteração posterior)
  Regra   : mesma lógica de resolvido/retry do pedido.

Deploy: Render — mesmo padrão do projeto FRI Matriz -> ATIVA.
"""

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

ETAPAS_MONITORADAS = {"10", "20", "50"}

# pedidos cuja gravação no Omie já retornou sem erro — pulam direto.
# Remessa NÃO tem cache: pode receber itens novos/corrigidos a qualquer momento,
# então toda alteração é reprocessada (com guard anti-loop por comparação).
_pedidos_resolvidos: set = set()


# ── UTILITÁRIOS ────────────────────────────────────────────────────────────────

def _calcular_fabricacao_validade(validade_raw: str):
    """Aceita MM/YYYY ou DD/MM/YYYY. Retorna (fabricacao, validade) em DD/MM/YYYY.
    Retorna ("", "") se a validade for inválida — evita 'dVal: - -' na NF-e."""
    if not validade_raw or str(validade_raw).strip() in ("S/V", "-", ""):
        return "", ""
    try:
        texto = str(validade_raw).strip()
        partes = texto.split("/")
        if len(partes) == 2:
            mes, ano = int(partes[0]), int(partes[1])
            if ano < 100:
                ano += 2000
            validade_dt = date(ano, mes, 1)
        else:
            validade_dt = datetime.strptime(texto, "%d/%m/%Y").date()

        fabricacao_dt = date(validade_dt.year - 3, validade_dt.month, validade_dt.day)
        return fabricacao_dt.strftime("%d/%m/%Y"), validade_dt.strftime("%d/%m/%Y")
    except Exception as e:
        print(f"⚠️ Erro ao converter validade '{validade_raw}': {e}")
        return "", ""


def _topico_match(payload: dict, esperado: str) -> bool:
    """Comparação de tópico case-insensitive e segura contra None."""
    topico = payload.get("topic") or ""
    return topico.lower() == esperado.lower()


def _tem_erro(resultado: dict) -> bool:
    """True se a resposta do Omie contém erro técnico (faultstring)."""
    return isinstance(resultado, dict) and "faultstring" in resultado


def _montar_rastreabilidade(lote, quantidade, validade_raw):
    """Monta o bloco de rastreabilidade, incluindo datas só se forem válidas."""
    fab, val = _calcular_fabricacao_validade(validade_raw)
    rast = {"numeroLote": lote, "qtdeProdutoLote": quantidade}
    if fab:
        rast["dataFabricacaoLote"] = fab
    if val:
        rast["dataValidadeLote"] = val
    return rast


# ── HEALTH CHECK ───────────────────────────────────────────────────────────────

@app.get("/")
def health():
    return {"status": "ok"}


# ── PEDIDO DE VENDA ────────────────────────────────────────────────────────────

def _processar_pedido(numero_pedido: str, app_key_origem: str):
    if numero_pedido in _pedidos_resolvidos:
        print(f"↪️ Pedido {numero_pedido} já resolvido. Ignorando.")
        return

    dados = consultar_pedido(numero_pedido, app_key_origem)
    if _tem_erro(dados):
        print(f"❌ Erro ao consultar pedido {numero_pedido}: {dados.get('faultstring')}")
        return

    cabecalho = dados.get("pedido_venda_produto", {}).get("cabecalho", {})
    codigo_pedido = cabecalho.get("codigo_pedido")
    itens = dados.get("pedido_venda_produto", {}).get("det", [])

    det_atualizado = []
    for idx, item in enumerate(itens, start=1):
        produto = item.get("produto", {})
        codigo_produto = produto.get("codigo_produto")
        codigo_item_integracao = item.get("ide", {}).get("codigo_item_integracao") or str(idx)

        _desc, sku = consultar_produto(codigo_produto, app_key_origem)

        lote, validade_raw = (None, None)
        if sku:
            lote, validade_raw = buscar_lote_validade(sku)

        if not lote:
            # SKU sem lote no Neon: descarta o item (não bloqueia, não retenta)
            continue

        det_atualizado.append({
            "ide": {"codigo_item_integracao": codigo_item_integracao},
            "rastreabilidade": _montar_rastreabilidade(lote, produto.get("quantidade"), validade_raw),
        })

    if not det_atualizado:
        print(f"↪️ Pedido {numero_pedido}: nenhum item com lote no Neon. Nada a gravar.")
        # marca como resolvido — não há o que gravar, não adianta retentar
        _pedidos_resolvidos.add(numero_pedido)
        return

    resultado = alterar_pedido_rastreabilidade(codigo_pedido, det_atualizado, app_key_origem)

    if _tem_erro(resultado):
        print(f"❌ Pedido {numero_pedido} falhou ao gravar: {resultado.get('faultstring')} — retry na próxima etapa.")
        return

    print(f"✅ Pedido {numero_pedido} gravado ({len(det_atualizado)} item(ns)): {resultado}")
    _pedidos_resolvidos.add(numero_pedido)


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
        print(f"↪️ Tópico '{payload.get('topic', '')}' ignorado.")
        return {"status": "ignorado", "motivo": "topico não monitorado"}

    evento = payload.get("event", {})
    numero_pedido = evento.get("numeroPedido")
    etapa = evento.get("etapa")
    app_key_origem = payload.get("appKey", "")

    if not numero_pedido:
        print("❌ numeroPedido não encontrado no payload.")
        return {"status": "ignorado", "motivo": "numeroPedido não encontrado"}

    if etapa not in ETAPAS_MONITORADAS:
        print(f"↪️ Pedido {numero_pedido} foi pra etapa {etapa}. Ignorando.")
        return {"status": "ignorado", "motivo": f"etapa {etapa} não monitorada"}

    try:
        _processar_pedido(numero_pedido, app_key_origem)
        return {"status": "ok"}
    except Exception as e:
        print(f"❌ Erro ao processar pedido {numero_pedido}: {e}")
        return {"status": "erro", "mensagem": str(e)}


# ── REMESSA ────────────────────────────────────────────────────────────────────

def _processar_remessa(cod_remessa: int, app_key_origem: str):
    # Sem cache: remessa pode receber itens novos/corrigidos a qualquer momento.
    # A proteção contra loop é feita por comparação (só grava se houver mudança).
    dados = consultar_remessa(cod_remessa, app_key_origem)
    if _tem_erro(dados):
        print(f"❌ Erro ao consultar remessa {cod_remessa}: {dados.get('faultstring')}")
        return

    cabec = dados.get("cabec", {})
    cod_cliente = cabec.get("nCodCli")
    numero_remessa = cabec.get("cNumeroRemessa", str(cod_remessa))

    itens = dados.get("produtos", [])
    if not itens:
        print(f"↪️ Remessa {numero_remessa} ainda sem produtos. Aguardando próxima alteração.")
        return

    produtos = []
    itens_com_lote = 0
    precisa_gravar = False

    for item in itens:
        codigo_produto = item.get("nCodProd")
        val_unit = item.get("nValUnit")

        # base do item — omite nValUnit se for 0/None (Omie rejeita com erro 302)
        item_base = {
            "nCodProd": codigo_produto,
            "nCodIt": item.get("nCodIt"),
            "nQtde": item.get("nQtde"),
        }
        if val_unit:
            item_base["nValUnit"] = val_unit

        _desc, sku = consultar_produto(codigo_produto, app_key_origem)

        lote, validade_raw = (None, None)
        if sku:
            lote, validade_raw = buscar_lote_validade(sku)

        if not lote:
            # SKU sem lote no Neon: mantém o item, sem rastreabilidade
            produtos.append(item_base)
            continue

        rast = _montar_rastreabilidade(lote, item.get("nQtde"), validade_raw)
        produtos.append({**item_base, "rastreabilidade": rast})
        itens_com_lote += 1

        # guard anti-loop: só marca "precisa gravar" se o lote atual do item
        # for diferente do que vamos gravar (evita regravar o que já está certo)
        lote_atual = (item.get("rastreabilidade") or {}).get("numeroLote", "")
        if str(lote_atual).strip() != str(lote).strip():
            precisa_gravar = True

    if itens_com_lote == 0:
        print(f"↪️ Remessa {numero_remessa}: nenhum item com lote no Neon. Nada a gravar.")
        return

    if not precisa_gravar:
        print(f"↪️ Remessa {numero_remessa}: lotes já gravados e corretos. Nada a fazer (anti-loop).")
        return

    resultado = alterar_remessa_rastreabilidade(cod_remessa, cod_cliente, produtos, app_key_origem)

    if _tem_erro(resultado):
        print(f"❌ Remessa {numero_remessa} falhou ao gravar: {resultado.get('faultstring')} — retry na próxima alteração.")
        return

    print(f"✅ Remessa {numero_remessa} gravada ({itens_com_lote} item(ns) com lote): {resultado}")


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
        print(f"↪️ Tópico '{payload.get('topic', '')}' ignorado.")
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