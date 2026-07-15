import os
import re
import time
import json
import threading
from typing import Optional, Tuple

import requests

APP_KEY_MATRIZ = os.environ["APP_KEY"]
APP_SECRET_MATRIZ = os.environ["APP_SECRET"]
APP_KEY_003 = os.environ["APP_KEY_003"]
APP_SECRET_003 = os.environ["APP_SECRET_003"]

URL_PEDIDO  = "https://app.omie.com.br/api/v1/produtos/pedido/"
URL_REMESSA = "https://app.omie.com.br/api/v1/produtos/remessa/"
URL_PRODUTO = "https://app.omie.com.br/api/v1/geral/produtos/"

_cache_produto = {}
APPKEYS_EMPRESAS = [APP_KEY_MATRIZ, APP_KEY_003]

# lock pra evitar varreduras simultâneas colidindo no rate limit do Omie
_varredura_lock = threading.Lock()


def _get_credenciais(app_key_origem: str):
    if app_key_origem == APP_KEY_003:
        return APP_KEY_003, APP_SECRET_003
    return APP_KEY_MATRIZ, APP_SECRET_MATRIZ


def _chamada_omie(url, payload, tentativas=4):
    for tentativa in range(1, tentativas + 1):
        try:
            response = requests.post(url, json=payload, timeout=60)
            resultado = response.json()
        except requests.exceptions.Timeout:
            print(f"⏱️ Timeout tentativa {tentativa}/{tentativas}. Aguardando 10s...")
            time.sleep(10)
            continue

        fault = resultado.get("faultstring", "")

        # rate limit — aguarda e retenta
        if fault.startswith("ERROR: Consumo redundante") or "MISUSE" in fault or "REDUNDANT" in fault:
            match = re.search(r"Aguarde (\d+) segundos", fault) or re.search(r"(\d+) segundos", fault)
            segundos = int(match.group(1)) if match else 6
            print(f"⚠️ Rate limit. Aguardando {segundos}s (tentativa {tentativa}/{tentativas})...")
            time.sleep(segundos)
            continue

        # colisão de requisição — aguarda e retenta
        if "Já existe uma requisição" in fault:
            print(f"⚠️ Colisão de requisição. Aguardando 15s (tentativa {tentativa}/{tentativas})...")
            time.sleep(15)
            continue

        return resultado

    print("❌ Todas as tentativas usadas, retornando último resultado")
    return resultado


# ── LISTAGENS (varredura de segurança) ────────────────────────────────────────

def adquirir_lock_varredura() -> bool:
    """Tenta adquirir o lock da varredura. Retorna False se já estiver rodando."""
    acquired = _varredura_lock.acquire(blocking=False)
    if not acquired:
        print("⚠️ Varredura já em execução, pulando este ciclo.")
    return acquired


def liberar_lock_varredura():
    try:
        _varredura_lock.release()
    except RuntimeError:
        pass


def listar_pedidos_nao_faturados(app_key_alvo: str) -> list:
    """Lista todos os pedidos não faturados de uma empresa, paginando até o fim."""
    app_key, app_secret = _get_credenciais(app_key_alvo)
    pedidos = []
    pagina = 1

    # filtra só os últimos 2 dias — evita varrer o histórico inteiro (665 páginas)
    from datetime import datetime, timedelta
    hoje = datetime.now()
    data_de = (hoje - timedelta(days=2)).strftime("%d/%m/%Y")

    while True:
        payload = {
            "call": "ListarPedidos",
            "app_key": app_key,
            "app_secret": app_secret,
            "param": [{
                "pagina": pagina,
                "registros_por_pagina": 50,
                "apenas_importado_api": "N",
                "filtrar_por_data_de": data_de,
            }],
        }
        retorno = _chamada_omie(URL_PEDIDO, payload)

        if "faultstring" in retorno:
            fault = retorno.get("faultstring", "")
            print(f"❌ ListarPedidos (pág {pagina}): {fault}")
            # "Não existem registros" = fim da listagem, não é erro real
            if "Não existem registros" in fault or "nao existem registros" in fault.lower():
                print(f"   ↪️ Fim da listagem na pág {pagina}.")
            break

        lote = retorno.get("pedido_venda_produto", [])
        for ped in lote:
            cab = ped.get("cabecalho", {})
            info = ped.get("infoCadastro", {})
            faturada = str(info.get("faturado", "")).upper()
            if faturada == "S":
                continue
            num = cab.get("numero_pedido")
            if num:
                pedidos.append({
                    "numero_pedido": num,
                    "etapa": cab.get("etapa"),
                })

        total_paginas = retorno.get("total_de_paginas", 1)
        print(f"   📄 ListarPedidos pág {pagina}/{total_paginas} — {len(lote)} registros")
        if pagina >= total_paginas:
            break
        pagina += 1
        time.sleep(1)  # pequena pausa entre páginas pra não acumular rate limit

    return pedidos


def listar_remessas_nao_faturadas(app_key_alvo: str) -> list:
    """Lista todas as remessas não faturadas de uma empresa, paginando até o fim."""
    app_key, app_secret = _get_credenciais(app_key_alvo)
    remessas = []
    pagina = 1

    from datetime import datetime, timedelta
    hoje = datetime.now()
    data_de = (hoje - timedelta(days=2)).strftime("%d/%m/%Y")

    while True:
        payload = {
            "call": "ListarRemessas",
            "app_key": app_key,
            "app_secret": app_secret,
            "param": [{
                "pagina": pagina,
                "registros_por_pagina": 50,
                "filtrar_por_data_de": data_de,
            }],
        }
        retorno = _chamada_omie(URL_REMESSA, payload)

        if "faultstring" in retorno:
            fault = retorno.get("faultstring", "")
            print(f"❌ ListarRemessas (pág {pagina}): {fault}")
            if "Não existem registros" in fault or "nao existem registros" in fault.lower():
                print(f"   ↪️ Fim da listagem.")
            break

        # tenta os nomes de campo mais comuns do retorno do Omie
        lote = (retorno.get("remessaCadastro")
                or retorno.get("cadastros")
                or retorno.get("remessas")
                or [])

        for rem in lote:
            cabec = rem.get("cabec", rem)
            faturada = str(cabec.get("cFaturada", cabec.get("faturada", ""))).upper()
            if faturada == "S":
                continue
            cod = cabec.get("nCodRem") or rem.get("nCodRem")
            if cod:
                remessas.append({"nCodRem": cod})

        total_paginas = (retorno.get("nTotPaginas")
                         or retorno.get("total_de_paginas")
                         or 1)
        print(f"   📄 ListarRemessas pág {pagina}/{total_paginas} — {len(lote)} registros")
        if pagina >= total_paginas:
            break
        pagina += 1
        time.sleep(1)

    return remessas


# ── PEDIDO DE VENDA ────────────────────────────────────────────────────────────

def consultar_pedido(numero_pedido, app_key_origem: str) -> dict:
    app_key, app_secret = _get_credenciais(app_key_origem)
    payload = {
        "call": "ConsultarPedido",
        "app_key": app_key,
        "app_secret": app_secret,
        "param": [{"numero_pedido": numero_pedido}],
    }
    return _chamada_omie(URL_PEDIDO, payload)


def alterar_pedido_rastreabilidade(codigo_pedido, det_atualizado, app_key_origem: str) -> dict:
    """Grava lote/validade nos itens e campos de frete via AlterarPedidoVenda."""
    app_key, app_secret = _get_credenciais(app_key_origem)
    payload = {
        "call": "AlterarPedidoVenda",
        "app_key": app_key,
        "app_secret": app_secret,
        "param": [{
            "cabecalho": {"codigo_pedido": codigo_pedido},
            "det": det_atualizado,
            "frete": {
                "especie_volumes": "CAIXAS",
                "marca_volumes": "LENVIE",
            },
        }],
    }
    return _chamada_omie(URL_PEDIDO, payload)


# ── REMESSA ────────────────────────────────────────────────────────────────────

def consultar_remessa(cod_remessa: int, app_key_origem: str) -> dict:
    app_key, app_secret = _get_credenciais(app_key_origem)
    payload = {
        "call": "ConsultarRemessa",
        "app_key": app_key,
        "app_secret": app_secret,
        "param": [{"nCodRem": cod_remessa}],
    }
    return _chamada_omie(URL_REMESSA, payload)


def alterar_remessa_rastreabilidade(cod_remessa: int, cod_cliente: int, produtos: list, app_key_origem: str) -> dict:
    """Grava lote/validade nos itens + especie/marca nos volumes."""
    app_key, app_secret = _get_credenciais(app_key_origem)
    payload = {
        "call": "AlterarRemessa",
        "app_key": app_key,
        "app_secret": app_secret,
        "param": [{
            "cabec": {
                "nCodRem": cod_remessa,
                "nCodCli": cod_cliente,
            },
            "frete": {
                "cEspVol": "CAIXAS",
                "cMarVol": "LENVIE",
            },
            "produtos": produtos,
        }],
    }
    return _chamada_omie(URL_REMESSA, payload)


# ── PRODUTO (compartilhado) ────────────────────────────────────────────────────

def consultar_produto(codigo_produto, app_key_origem: str) -> Tuple[Optional[str], Optional[str]]:
    """Retorna (descricao, sku). Cacheado em memória."""
    cache_key = f"{app_key_origem}:{codigo_produto}"
    if cache_key in _cache_produto:
        return _cache_produto[cache_key]

    app_key, app_secret = _get_credenciais(app_key_origem)
    payload = {
        "call": "ConsultarProduto",
        "app_key": app_key,
        "app_secret": app_secret,
        "param": [{"codigo_produto": codigo_produto}],
    }
    retorno = _chamada_omie(URL_PRODUTO, payload)

    if "faultstring" in retorno:
        print(f"❌ Erro ao consultar produto {codigo_produto}: {retorno.get('faultstring')}")
        return None, None

    resultado = (retorno.get("descricao"), retorno.get("codigo"))
    _cache_produto[cache_key] = resultado
    return resultado
