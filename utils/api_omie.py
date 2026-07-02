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

    produtos: lista de dicts com nCodProd, nCodIt, nQtde, nValUnit, rastreabilidade.

    Nota: os nomes dos campos de especie/marca no frete da remessa precisam ser
    confirmados via teste (podem ser cEmbalagem/cMarca ou especie_volumes/marca_volumes
    igual ao pedido). O log do JSON enviado vai mostrar se o Omie aceitou.
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
                "cEspecie": "CAIXAS",
                "cMarca":   "LENVIE",
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