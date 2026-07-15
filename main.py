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

import os
from collections import deque
from datetime import date, datetime

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from utils.api_omie import (
    consultar_pedido,
    consultar_produto,
    alterar_pedido_rastreabilidade,
    consultar_remessa,
    alterar_remessa_rastreabilidade,
    listar_pedidos_nao_faturados,
    listar_remessas_nao_faturadas,
    APPKEYS_EMPRESAS,
    adquirir_lock_varredura,
    liberar_lock_varredura,
)
from utils.neon_select import buscar_lote_validade

app = FastAPI()

# token pra proteger o endpoint de varredura (configurar no Render)
VARREDURA_TOKEN = os.environ.get("VARREDURA_TOKEN", "")

ETAPAS_MONITORADAS = {"10", "20", "50"}

# pedidos cuja gravação no Omie já retornou sem erro — pulam direto.
# Remessa NÃO tem cache: pode receber itens novos/corrigidos a qualquer momento,
# então toda alteração é reprocessada (com guard anti-loop por comparação).
_pedidos_resolvidos: set = set()

# dedup por messageId: o Omie reentrega o mesmo evento até receber 200 rápido.
# Guardamos os últimos IDs vistos pra descartar reentregas antes de processar.
_mensagens_vistas: set = set()
_mensagens_ordem: deque = deque(maxlen=2000)


def _ja_processada(message_id: str) -> bool:
    """True se esse messageId já foi recebido antes (reentrega do Omie)."""
    if not message_id:
        return False
    if message_id in _mensagens_vistas:
        return True
    _mensagens_vistas.add(message_id)
    # mantém o set limitado: quando a deque estoura, remove o ID mais antigo do set
    if len(_mensagens_ordem) == _mensagens_ordem.maxlen:
        antigo = _mensagens_ordem[0]
        _mensagens_vistas.discard(antigo)
    _mensagens_ordem.append(message_id)
    return False


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

def _processar_pedido(numero_pedido: str, app_key_origem: str, forcar: bool = False):
    # a varredura usa forcar=True pra reprocessar mesmo o que já está no cache
    if not forcar and numero_pedido in _pedidos_resolvidos:
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

        print(f"   🔍 cod={codigo_produto} | sku='{sku}' | lote='{lote}'")

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
async def webhook_etapa_pedido(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    message_id = payload.get("messageId", "")
    if _ja_processada(message_id):
        # reentrega do mesmo evento — descarta sem processar
        return {"status": "duplicado"}

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

    # responde 200 imediatamente; o processamento pesado roda em background
    background_tasks.add_task(_processar_pedido_seguro, numero_pedido, app_key_origem)
    return {"status": "aceito"}


def _processar_pedido_seguro(numero_pedido: str, app_key_origem: str):
    """Wrapper que captura exceções pra não derrubar a background task."""
    try:
        _processar_pedido(numero_pedido, app_key_origem)
    except Exception as e:
        print(f"❌ Erro ao processar pedido {numero_pedido}: {e}")


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
async def webhook_remessa_criada(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()

    message_id = payload.get("messageId", "")
    if _ja_processada(message_id):
        return {"status": "duplicado"}

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

    background_tasks.add_task(_processar_remessa_seguro, int(cod_remessa), app_key_origem)
    return {"status": "aceito"}


def _processar_remessa_seguro(cod_remessa: int, app_key_origem: str):
    """Wrapper que captura exceções pra não derrubar a background task."""
    try:
        _processar_remessa(cod_remessa, app_key_origem)
    except Exception as e:
        print(f"❌ Erro ao processar remessa {cod_remessa}: {e}")


# ── VARREDURA DE SEGURANÇA ─────────────────────────────────────────────────────
# Rede de segurança: lista tudo que está não faturado (pedidos e remessas, nas
# duas empresas) e garante lote/validade, independente de etapa ou de webhook.
# Disparada por cron externo (cron-job.org) a cada 5 min.

def _rodar_varredura():
    # evita execuções simultâneas que colidem no rate limit do Omie
    if not adquirir_lock_varredura():
        return
    try:
        _rodar_varredura_interna()
    finally:
        liberar_lock_varredura()


def _rodar_varredura_interna():
    print("========== INÍCIO DA VARREDURA ==========")
    total_pedidos = 0
    total_remessas = 0

    for app_key in APPKEYS_EMPRESAS:
        # ── pedidos não faturados ──
        try:
            pedidos = listar_pedidos_nao_faturados(app_key)
            print(f"🔎 Empresa {app_key[:6]}...: {len(pedidos)} pedido(s) não faturado(s).")
            for ped in pedidos:
                numero = ped.get("numero_pedido")
                if not numero:
                    continue
                try:
                    # forcar=True: varredura sempre reavalia, não confia no cache
                    _processar_pedido(str(numero), app_key, forcar=True)
                    total_pedidos += 1
                except Exception as e:
                    print(f"❌ Varredura pedido {numero}: {e}")
        except Exception as e:
            print(f"❌ Erro ao listar pedidos da empresa {app_key[:6]}...: {e}")

        # ── remessas não faturadas ──
        try:
            remessas = listar_remessas_nao_faturadas(app_key)
            print(f"🔎 Empresa {app_key[:6]}...: {len(remessas)} remessa(s) não faturada(s).")
            for rem in remessas:
                cod = rem.get("nCodRem")
                if not cod:
                    continue
                try:
                    _processar_remessa(int(cod), app_key)
                    total_remessas += 1
                except Exception as e:
                    print(f"❌ Varredura remessa {cod}: {e}")
        except Exception as e:
            print(f"❌ Erro ao listar remessas da empresa {app_key[:6]}...: {e}")

    print(f"========== FIM DA VARREDURA ({total_pedidos} pedidos, {total_remessas} remessas) ==========")


@app.get("/varredura")
@app.post("/varredura")
async def varredura(request: Request, background_tasks: BackgroundTasks):
    # proteção por token: só dispara com o token certo
    token = request.query_params.get("token", "")
    if VARREDURA_TOKEN and token != VARREDURA_TOKEN:
        return JSONResponse(status_code=403, content={"status": "token inválido"})

    # roda em background pra responder rápido ao cron
    background_tasks.add_task(_rodar_varredura)
    return {"status": "varredura iniciada"}


# ── DIAGNÓSTICO (temporário) ───────────────────────────────────────────────────

@app.get("/diagnostico/sku")
async def diagnostico_sku(request: Request, sku: str):
    """
    Diagnóstico pontual de um SKU.
    Uso: /diagnostico/sku?sku=10142200&token=SEU_TOKEN
    Mostra o que o Omie retorna no ConsultarProduto e o que está no Neon.
    """
    token = request.query_params.get("token", "")
    if VARREDURA_TOKEN and token != VARREDURA_TOKEN:
        return JSONResponse(status_code=403, content={"status": "token inválido"})

    resultado = {}

    # busca no Neon
    try:
        lote, validade = buscar_lote_validade(sku)
        fab, val = _calcular_fabricacao_validade(validade or "")
        resultado["neon"] = {
            "sku_buscado": sku,
            "lote": lote,
            "validade_raw": validade,
            "fabricacao_calculada": fab,
            "validade_calculada": val,
        }
    except Exception as e:
        resultado["neon"] = {"erro": str(e)}

    return resultado


@app.get("/diagnostico/pedido")
async def diagnostico_pedido(request: Request, numero: str, empresa: str = "matriz"):
    """
    Diagnóstico de um pedido completo.
    Uso: /diagnostico/pedido?numero=34627&empresa=matriz&token=SEU_TOKEN
         empresa: 'matriz' ou '003'
    Mostra cada item do pedido, o SKU retornado pelo Omie e o lote encontrado no Neon.
    """
    token = request.query_params.get("token", "")
    if VARREDURA_TOKEN and token != VARREDURA_TOKEN:
        return JSONResponse(status_code=403, content={"status": "token inválido"})

    from utils.api_omie import APP_KEY_003, APP_KEY_MATRIZ
    app_key = APP_KEY_003 if empresa == "003" else APP_KEY_MATRIZ

    dados = consultar_pedido(numero, app_key)
    if _tem_erro(dados):
        return {"erro": dados.get("faultstring")}

    itens = dados.get("pedido_venda_produto", {}).get("det", [])
    resultado = []

    for idx, item in enumerate(itens, start=1):
        produto = item.get("produto", {})
        codigo_produto = produto.get("codigo_produto")
        qtd = produto.get("quantidade")

        desc, sku = consultar_produto(codigo_produto, app_key)

        lote, validade = (None, None)
        if sku:
            lote, validade = buscar_lote_validade(sku)

        fab, val = _calcular_fabricacao_validade(validade or "")

        resultado.append({
            "item": idx,
            "codigo_produto_omie": codigo_produto,
            "descricao_omie": desc,
            "sku_retornado_omie": sku,
            "quantidade": qtd,
            "neon_lote": lote,
            "neon_validade_raw": validade,
            "fabricacao_calculada": fab,
            "validade_calculada": val,
            "vai_gravar": bool(lote),
        })

    return {
        "numero_pedido": numero,
        "empresa": empresa,
        "total_itens": len(itens),
        "itens": resultado,
    }
