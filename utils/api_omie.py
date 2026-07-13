import os
import re
import time
import json
from typing import Optional, Tuple

import requests

APP_KEY_MATRIZ = os.environ["APP_KEY"]
APP_SECRET_MATRIZ = os.environ["APP_SECRET"]
APP_KEY_003 = os.environ["APP_KEY_003"]
APP_SECRET_003 = os.environ["APP_SECRET_003"]

URL_PEDIDO  = "https://app.omie.com.br/api/v1/produtos/pedido/"
URL_REMESSA = "https://app.omie.com.br/api/v1/produtos/remessa/"
URL_PRODUTO = "https://app.omie.com.br/api/v1/geral/produtos/"

# cache simples em memória pra não bater 2x no mesmo produto durante um pedido
_cache_produto = {}

# lista das empresas a varrer (chave usada pelo _get_credenciais)
APPKEYS_EMPRESAS = [APP_KEY_MATRIZ, APP_KEY_003]


def _get_credenciais(app_key_origem: str):
    """Retorna (app_key, app_secret) corretos com base no appKey que veio no payload."""
    if app_key_origem == APP_KEY_003:
        return APP_KEY_003, APP_SECRET_003
    return APP_KEY_MATRIZ, APP_SECRET_MATRIZ


def _chamada_omie(url, payload, tentativas=4):
    """Wrapper genérico com retry em rate limit e timeout, igual ao padrão que você já usa."""
    for tentativa in range(1, tentativas + 1):
        try:
            response = requests.post(url, json=payload, timeout=60)
            resultado = response.json()
        except requests.exceptions.Timeout:
            print(f"⏱️ Timeout na tentativa {tentativa}/{tentativas}. Aguardando 10s antes de tentar novamente...")
            time.sleep(10)
            continue

        fault = resultado.get("faultstring", "")
        if fault.startswith("ERROR: Consumo redundante") or "MISUSE" in fault or "REDUNDANT" in fault:
            match = re.search(r"Aguarde (\d+) segundos", fault) or re.search(r"(\d+) segundos", fault)
            segundos = int(match.group(1)) if match else 6
            print(f"⚠️ Rate limit. Aguardando {segundos}s (tentativa {tentativa}/{tentativas})...")
            time.sleep(segundos)
            continue

        return resultado

    print("❌ Todas as tentativas usadas, retornando último resultado")
    return resultado


# ── LISTAGENS (para a varredura de segurança) ──────────────────────────────────

def listar_pedidos_nao_faturados(app_key_alvo: str) -> list:
    """
    Lista TODOS os pedidos não faturados de uma empresa, paginando até o fim.
    Retorna lista de dicts com pelo menos numero_pedido e etapa.
    Filtro: apenas_importado_api = "N" (todos), faturada tratada no retorno.
    """
    app_key, app_secret = _get_credenciais(app_key_alvo)
    pedidos = []
    pagina = 1

    while True:
        payload = {
            "call": "ListarPedidos",
            "app_key": app_key,
            "app_secret": app_secret,
            "param": [{
                "pagina": pagina,
                "registros_por_pagina": 100,
                "apenas_importado_api": "N",
            }],
        }
        retorno = _chamada_omie(URL_PEDIDO, payload)

        if "faultstring" in retorno:
            print(f"❌ ListarPedidos (pág {pagina}): {retorno.get('faultstring')}")
            break

        lote = retorno.get("pedido_venda_produto", [])
        for ped in lote:
            cab = ped.get("cabecalho", {})
            info = ped.get("infoCadastro", {})
            # considera não faturado se a flag de faturado não for "S"
            faturada = str(info.get("faturado", "")).upper()
            if faturada == "S":
                continue
            pedidos.append({
                "numero_pedido": cab.get("numero_pedido"),
                "etapa": cab.get("etapa"),
            })

        total_paginas = retorno.get("total_de_paginas", 1)
        if pagina >= total_paginas:
            break
        pagina += 1

    return pedidos


def listar_remessas_nao_faturadas(app_key_alvo: str) -> list:
    """
    Lista TODAS as remessas não faturadas de uma empresa, paginando até o fim.
    Retorna lista de dicts com nCodRem.
    """
    app_key, app_secret = _get_credenciais(app_key_alvo)
    remessas = []
    pagina = 1

    while True:
        payload = {
            "call": "ListarRemessas",
            "app_key": app_key,
            "app_secret": app_secret,
            "param": [{
                "nPagina": pagina,
                "nRegPorPagina": 100,
            }],
        }
        retorno = _chamada_omie(URL_REMESSA, payload)

        if "faultstring" in retorno:
            print(f"❌ ListarRemessas (pág {pagina}): {retorno.get('faultstring')}")
            break

        lote = retorno.get("remessaCadastro", retorno.get("cadastros", []))
        for rem in lote:
            cabec = rem.get("cabec", rem)
            faturada = str(cabec.get("cFaturada", cabec.get("faturada", ""))).upper()
            if faturada == "S":
                continue
            cod = cabec.get("nCodRem") or rem.get("nCodRem")
            if cod:
                remessas.append({"nCodRem": cod})

        total_paginas = retorno.get("nTotPaginas", retorno.get("total_de_paginas", 1))
        if pagina >= total_paginas:
            break
        pagina += 1

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

    print("\n===== JSON ENVIADO (AlterarPedidoVenda) =====")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("==============================================")

    return _chamada_omie(URL_PEDIDO, payload)


# ── REMESSA ────────────────────────────────────────────────────────────────────

def consultar_remessa(cod_remessa: int, app_key_origem: str) -> dict:
    """Consulta detalhes completos de uma remessa pelo código interno Omie."""
    app_key, app_secret = _get_credenciais(app_key_origem)
    payload = {
        "call": "ConsultarRemessa",
        "app_key": app_key,
        "app_secret": app_secret,
        "param": [{"nCodRem": cod_remessa}],
    }
    return _chamada_omie(URL_REMESSA, payload)


def alterar_remessa_rastreabilidade(cod_remessa: int, cod_cliente: int, produtos: list, app_key_origem: str) -> dict:
    """
    Grava lote/validade nos itens da remessa + especie/marca nos volumes.
    Campos de frete confirmados via ConsultarRemessa: cEspVol e cMarVol.
    """
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

    print("\n===== JSON ENVIADO (AlterarRemessa) =====")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    print("=========================================")

    return _chamada_omie(URL_REMESSA, payload)


# ── PRODUTO (compartilhado) ────────────────────────────────────────────────────

def consultar_produto(codigo_produto, app_key_origem: str) -> Tuple[Optional[str], Optional[str]]:
    """Retorna (descricao, sku). Cacheado em memória durante o processo."""
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
