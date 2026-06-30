import os
import psycopg2

CONN_STRING = os.environ["NEON_DB_URL"]


def buscar_lote_validade(sku: str):
    """Busca lote/validade de um SKU específico no Neon (tblotematriz)."""
    sku_norm = str(sku).strip().upper()

    with psycopg2.connect(CONN_STRING) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lote, validade FROM tblotematriz WHERE sku = %s",
                (sku_norm,),
            )
            row = cur.fetchone()

    if not row:
        return None, None

    lote, validade = row
    return (lote or "").strip(), (validade or "").strip()
