import os
import re
import time
import json
from typing import Optional, Tuple

import requests

APP_KEY = os.environ["APP_KEY"]
APP_SECRET = os.environ["APP_SECRET"]

URL_PEDIDO = "https://app.omie.com.br/api/v1/produtos/pedido/"
URL_PRODUTO = "https://app.omie.com.br/api/v1/geral/produtos/"

# cache simples em memória pra não bater 2x no mesmo produto durante um pedido
_cache_produto = {}


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


def consultar_pedido(numero_pedido) -> dict:
    payload = {
        "call": "ConsultarPedido",
        "app_key": APP_KEY,
        "app_secret": APP_SECRET,
        "param": [{"numero_pedido": numero_pedido}],
    }
    return _chamada_omie(URL_PEDIDO, payload)


def consultar_produto(codigo_produto) -> Tuple[Optional[str], Optional[str]]:
    """Retorna (descricao, sku). Cacheado em memória durante o processo."""
    if codigo_produto in _cache_produto:
        return _cache_produto[codigo_produto]

    payload = {
        "call": "ConsultarProduto",
        "app_key": APP_KEY,
        "app_secret": APP_SECRET,
        "param": [{"codigo_produto": codigo_produto}],
    }
    retorno = _chamada_omie(URL_PRODUTO, payload)

    if "faultstring" in retorno:
        print(f"❌ Erro ao consultar produto {codigo_produto}: {retorno.get('faultstring')}")
        return None, None

    resultado = (retorno.get("descricao"), retorno.get("codigo"))
    _cache_produto[codigo_produto] = resultado
    return resultado


def alterar_pedido_rastreabilidade(codigo_pedido, det_atualizado) -> dict:
    """Grava lote/validade nos itens e campos de frete via AlterarPedidoVenda."""
    payload = {
        "call": "AlterarPedidoVenda",
        "app_key": APP_KEY,
        "app_secret": APP_SECRET,
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