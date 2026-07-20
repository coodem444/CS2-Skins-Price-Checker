#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║         CS2 Notion Price Updater  ·  v3.2                            ║
║  Steam Market + Skinport + CSFloat · Discord · SQLite · Auto-setup   ║
╠══════════════════════════════════════════════════════════════════════╣
║  Necesitas:  pip install --upgrade requests python-dotenv            ║
║  (notion-client eliminado — usamos la REST API directamente)         ║
║  ⚠  Credenciales en .env — NUNCA las escribas en este fichero        ║
╚══════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("❌  Instala requests:  pip install --upgrade requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    print("❌  Instala python-dotenv:  pip install --upgrade python-dotenv")
    sys.exit(1)

load_dotenv(Path(__file__).parent / ".env")


# ══════════════════════════════════════════════════════════════════════
#  ▶  CONFIGURACIÓN  ◀
#  Los secretos NUNCA van aquí — se cargan desde el fichero .env
#  (copia .env.example → .env y rellena tus valores).
# ══════════════════════════════════════════════════════════════════════

NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
DATABASE_ID  = os.getenv("NOTION_DATABASE_ID", "37c76565bb44800a8ba2f65c4f0b5e3f")

DISCORD_WEBHOOK    = os.getenv("DISCORD_WEBHOOK", "")
CSFLOAT_API_KEY    = os.getenv("CSFLOAT_API_KEY", "")

ROI_ALERT_THRESHOLD    = 20    # % ROI para alerta Discord
PRICE_CHANGE_THRESHOLD = 10    # % cambio 24h para alerta Discord
ALERT_COOLDOWN_HORAS   = 24   # Horas mínimas entre alertas repetidas

# True  → "Coste Compra" es precio por unidad  (Inversión = Coste × Qty)
# False → "Coste Compra" es el coste total de toda la posición
COSTE_POR_UNIDAD = True

STEAM_CURRENCY = 3      # 3=EUR  1=USD  2=GBP
REQUEST_DELAY  = 1.5    # seg. entre llamadas a Steam

FETCH_IMAGES  = True    # Busca imagen en Steam CDN solo si columna está vacía
SETUP_COLUMNS = True    # Crea columnas extra en Notion en la primera ejecución
DRY_RUN       = False   # True = leer precios pero NO escribir en Notion

# ── CSFloat ──────────────────────────────────────────────────────────
CSFLOAT_API            = "https://csfloat.com/api/v1"
CSFLOAT_TIMEOUT        = 15
CSFLOAT_MAX_REINTENTOS = 4      # nº de intentos ante 429 / 5xx / timeout
CSFLOAT_BACKOFF_BASE   = 2      # segundos, backoff exponencial: base * 2^n
CSFLOAT_REQUEST_DELAY  = 1.0    # seg. entre llamadas a CSFloat

# CSFloat devuelve precios en céntimos de USD (no ofrece parámetro de
# moneda). Convertimos a EUR con la tasa BCE (vía Frankfurter, sin API
# key). Si la consulta de tasa falla, se usa este valor de reserva —
# actualízalo de vez en cuando si dejas de tener conexión a Frankfurter.
TASA_USD_EUR_FALLBACK = 0.92

DB_PATH = Path(__file__).parent / "historial_precios.db"

# Notion REST API — versión fija, estable a largo plazo
NOTION_API = "https://api.notion.com/v1"
NOTION_VER = "2022-06-28"

# ══════════════════════════════════════════════════════════════════════


SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent":      "CS2NotionUpdater/3.2",
    "Accept":          "application/json",
    "Accept-Encoding": "gzip, deflate",
})
_skinport: dict = {}

# Estado en memoria para CSFloat (vive solo durante una ejecución)
_csfloat_cache: dict[str, float | None] = {}   # nombre_normalizado → precio EUR (o None)
_csfloat_activo: bool = bool(CSFLOAT_API_KEY)  # se desactiva solo si el 401 confirma key inválida
_tasa_usd_eur: float | None = None


def validar_configuracion() -> None:
    """
    Comprueba la configuración mínima antes de arrancar.
    NOTION_TOKEN y DATABASE_ID son obligatorios (sin ellos no hay nada
    que hacer). CSFLOAT_API_KEY es opcional: si falta, esa fuente se
    omite y el resto del script sigue funcionando exactamente igual
    (su estado ya se refleja en el banner de arranque).
    """
    if not NOTION_TOKEN:
        print("❌  Falta NOTION_TOKEN. Crea un fichero .env (ver .env.example) con tu token de integración.")
        sys.exit(1)
    if not DATABASE_ID:
        print("❌  Falta NOTION_DATABASE_ID en .env.")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────
#  UTILIDADES
# ─────────────────────────────────────────────────────────────────────

def parsear_precio(raw: str) -> float | None:
    """
    Convierte cadenas de precio a float.
    Soporta:  '1,50€'  '1.234,56€'  '$1,234.56'  '150.00'
    """
    s  = re.sub(r"[€$£\s\xa0]", "", str(raw)).strip()
    lc = s.rfind(",")
    ld = s.rfind(".")
    if lc > ld:                          # coma decimal europea: 1.234,56
        s = s.replace(".", "").replace(",", ".")
    elif ld > lc and lc != -1:           # punto decimal: 1,234.56
        s = s.replace(",", "")
    try:
        return round(float(s), 2)
    except (ValueError, AttributeError):
        return None


_ESPACIOS_MULTIPLES = re.compile(r"\s+")
_COMILLAS = str.maketrans({
    "\u2018": "'", "\u2019": "'",   # comillas simples tipográficas
    "\u201c": '"', "\u201d": '"',  # comillas dobles tipográficas
    "\u2013": "-", "\u2014": "-",  # en dash / em dash → guion
})


def normalizar_nombre(nombre: str) -> str:
    """
    Normaliza un market_hash_name para maximizar el acierto al consultar
    APIs externas (CSFloat, y por extensión cualquier fuente futura).

    Resuelve de forma segura: unicode compuesto/descompuesto (NFC),
    espacios duplicados o no separables (\\xa0), comillas y guiones
    tipográficos, y espaciado irregular alrededor del separador '|'.

    Deliberadamente NO intenta "adivinar" StatTrak™/Souvenir cuando el
    símbolo falta, ni hace fuzzy-matching contra un catálogo: en un
    tracker de inversión un match incorrecto es peor que un match nulo.
    """
    if not nombre:
        return nombre
    n = unicodedata.normalize("NFC", nombre)
    n = n.translate(_COMILLAS)
    n = n.replace("\xa0", " ")
    n = _ESPACIOS_MULTIPLES.sub(" ", n).strip()
    n = re.sub(r"\s*\|\s*", " | ", n)   # separador '|' con espaciado consistente
    return n


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sep(n: int = 68) -> str:
    return "═" * n


# ─────────────────────────────────────────────────────────────────────
#  NOTION REST API  ← sin notion-client, requests directamente
#  Ventaja: no depende de versiones de librería, estable para siempre
# ─────────────────────────────────────────────────────────────────────

def _nh() -> dict:
    """Headers de autenticación para Notion API."""
    return {
        "Authorization":  f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VER,
        "Content-Type":   "application/json",
    }


def notion_query_db(cursor: str | None = None) -> dict:
    """Consulta la base de datos (POST /databases/{id}/query)."""
    body: dict = {"page_size": 100}
    if cursor:
        body["start_cursor"] = cursor
    r = SESSION.post(
        f"{NOTION_API}/databases/{DATABASE_ID}/query",
        headers=_nh(), json=body, timeout=15,
    )
    r.raise_for_status()
    return r.json()


def notion_update_page(page_id: str, props: dict):
    """Actualiza propiedades de una página (PATCH /pages/{id})."""
    SESSION.patch(
        f"{NOTION_API}/pages/{page_id}",
        headers=_nh(), json={"properties": props}, timeout=15,
    ).raise_for_status()


def notion_get_db() -> dict:
    """Obtiene metadatos de la BD (GET /databases/{id})."""
    r = SESSION.get(
        f"{NOTION_API}/databases/{DATABASE_ID}",
        headers=_nh(), timeout=15,
    )
    r.raise_for_status()
    return r.json()


def notion_update_db(props: dict):
    """Añade columnas a la BD (PATCH /databases/{id})."""
    SESSION.patch(
        f"{NOTION_API}/databases/{DATABASE_ID}",
        headers=_nh(), json={"properties": props}, timeout=15,
    ).raise_for_status()


# ─────────────────────────────────────────────────────────────────────
#  STEAM MARKET API
# ─────────────────────────────────────────────────────────────────────

def precio_steam(nombre: str, reintentos: int = 3) -> float | None:
    """Precio más bajo en Steam Community Market (CS2)."""
    url = "https://steamcommunity.com/market/priceoverview/"
    for n in range(1, reintentos + 1):
        try:
            r = SESSION.get(url, params={
                "currency":         STEAM_CURRENCY,
                "appid":            730,
                "market_hash_name": nombre,
            }, timeout=10)

            if r.status_code == 429:
                w = 30 * n
                print(f"\n    ⏳  Rate-limit Steam. Esperando {w}s…")
                time.sleep(w)
                continue

            r.raise_for_status()
            d = r.json()
            if not d.get("success"):
                return None
            raw = d.get("lowest_price") or d.get("median_price") or ""
            return parsear_precio(raw) if raw else None

        except Exception as e:
            print(f"\n    ⚠  Steam intento {n}/{reintentos}: {e}")
            if n < reintentos:
                time.sleep(3)
    return None


def imagen_steam(nombre: str) -> str | None:
    """
    URL de imagen del item en Steam CDN.
    Solo se llama si la columna 'Imagen' está vacía — las siguientes
    ejecuciones no hacen ninguna llamada adicional.
    """
    try:
        r = SESSION.get(
            "https://steamcommunity.com/market/search/render/",
            params={
                "query":    nombre,
                "appid":    730,
                "count":    1,
                "format":   "json",
                "currency": STEAM_CURRENCY,
            },
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("results", [])
        if items:
            icon = items[0].get("asset_description", {}).get("icon_url", "")
            if icon:
                return (
                    "https://community.cloudflare.steamstatic.com"
                    f"/economy/image/{icon}/256fx256f"
                )
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────────────────────────────
#  SKINPORT API  (bulk — 1 sola llamada para todas las skins)
#  FIX v3.1: Accept header correcto + reintentos con params alternativos
# ─────────────────────────────────────────────────────────────────────

def cargar_skinport() -> bool:
    """
    Descarga todos los precios de CS2 de Skinport de una vez.
    Prueba varias combinaciones de parámetros por si la API cambia.
    Si falla, el script continúa usando solo Steam.
    """
    global _skinport
    print("  📦  Skinport bulk…", end="", flush=True)

    # Cabeceras obligatorias para que Skinport no devuelva 406
    skinport_headers = {
        "Accept":          "application/json",
        "Accept-Encoding": "gzip, deflate",
        "User-Agent":      "CS2NotionUpdater/3.2",
    }
    # Intentamos primero con tradable=1, luego sin él
    param_sets = [
        {"app_id": 730, "currency": "EUR", "tradable": 1},
        {"app_id": 730, "currency": "EUR"},
    ]

    for params in param_sets:
        try:
            r = SESSION.get(
                "https://api.skinport.com/v1/items",
                params=params,
                headers=skinport_headers,
                timeout=30,
            )
            if r.status_code in (401, 403, 406):
                # 406 = Accept header mal; 401/403 = requiere API key
                continue
            r.raise_for_status()

            _skinport = {
                it["market_hash_name"]: it["min_price"]
                for it in r.json()
                if it.get("min_price") is not None
            }
            print(f"  ✅  {len(_skinport):,} ítems cargados")
            return True

        except requests.exceptions.Timeout:
            print("\n    ⚠  Skinport timeout")
        except Exception as e:
            print(f"\n    ⚠  Skinport error: {e}")

    print("  ⚠  No disponible — usando solo Steam")
    return False


def precio_skinport(nombre: str) -> float | None:
    return _skinport.get(nombre)


# ─────────────────────────────────────────────────────────────────────
#  CONVERSIÓN DE DIVISA  (CSFloat devuelve céntimos de USD)
# ─────────────────────────────────────────────────────────────────────

def tasa_usd_eur() -> float:
    """
    Tasa USD→EUR, cacheada durante toda la ejecución (1 sola llamada).
    Fuente: Frankfurter (datos BCE, sin API key). Ante cualquier fallo,
    cae de forma segura al valor de reserva — nunca bloquea el script.
    """
    global _tasa_usd_eur
    if _tasa_usd_eur is not None:
        return _tasa_usd_eur
    try:
        r = SESSION.get(
            "https://api.frankfurter.dev/v1/latest",
            params={"base": "USD", "symbols": "EUR"},
            timeout=8,
        )
        r.raise_for_status()
        _tasa_usd_eur = float(r.json()["rates"]["EUR"])
        print(f"  💱  Tasa USD→EUR: {_tasa_usd_eur:.4f} (Frankfurter/BCE)")
    except Exception as e:
        _tasa_usd_eur = TASA_USD_EUR_FALLBACK
        print(f"  ⚠  Tasa USD→EUR no disponible, uso reserva {TASA_USD_EUR_FALLBACK}: {e}")
    return _tasa_usd_eur


# ─────────────────────────────────────────────────────────────────────
#  CSFLOAT API  (docs.csfloat.com)
#  Auth: header  Authorization: <API_KEY>  (sin prefijo "Bearer")
#  GET /v1/listings?market_hash_name=...&sort_by=lowest_price&limit=1
#  Precio devuelto en céntimos de USD → se convierte a EUR.
# ─────────────────────────────────────────────────────────────────────

def _csfloat_headers() -> dict:
    return {
        "Authorization": CSFLOAT_API_KEY,
        "Accept":        "application/json",
    }


def _consultar_csfloat(clave: str) -> float | None:
    """Hace la(s) llamada(s) HTTP reales a CSFloat con reintentos/backoff.
    No toca la caché — eso lo gestiona el llamador (precio_csfloat)."""
    global _csfloat_activo

    for intento in range(1, CSFLOAT_MAX_REINTENTOS + 1):
        try:
            r = SESSION.get(
                f"{CSFLOAT_API}/listings",
                headers=_csfloat_headers(),
                params={
                    "market_hash_name": clave,
                    "sort_by":          "lowest_price",
                    "type":             "buy_now",
                    "limit":            1,
                },
                timeout=CSFLOAT_TIMEOUT,
            )

            if r.status_code == 401:
                print("    ❌  Error CSFloat: 401 — API key inválida. Se desactiva CSFloat para el resto de la ejecución.")
                _csfloat_activo = False
                return None

            if r.status_code == 403:
                print("    ❌  Error CSFloat: 403 — sin permiso para este recurso.")
                return None

            if r.status_code == 404:
                return None  # sin listings activos — no es un error

            if r.status_code == 429:
                espera = int(r.headers.get("Retry-After", 0)) or CSFLOAT_BACKOFF_BASE * (2 ** (intento - 1))
                print(f"    ⏳  Retry CSFloat (429): esperando {espera}s…")
                time.sleep(espera)
                continue

            if r.status_code >= 500:
                espera = CSFLOAT_BACKOFF_BASE * (2 ** (intento - 1))
                print(f"    🔁  Retry CSFloat ({r.status_code}): esperando {espera}s…")
                time.sleep(espera)
                continue

            r.raise_for_status()
            payload = r.json()
            # La API envuelve los resultados en {"data": [...], "cursor": ...}
            # pero se acepta también una lista plana por robustez ante
            # cambios futuros de formato.
            listings = payload.get("data", []) if isinstance(payload, dict) else payload
            if not listings:
                return None

            centavos_usd = listings[0].get("price")
            if centavos_usd is None:
                return None

            precio_eur = round((centavos_usd / 100) * tasa_usd_eur(), 2)
            print(f"    ✅  Precio encontrado en CSFloat: {precio_eur:.2f}€")
            return precio_eur

        except requests.exceptions.Timeout:
            print(f"    ⚠  Timeout CSFloat (intento {intento}/{CSFLOAT_MAX_REINTENTOS})")
        except Exception as e:
            print(f"    ⚠  Error CSFloat (intento {intento}/{CSFLOAT_MAX_REINTENTOS}): {e}")

        if intento < CSFLOAT_MAX_REINTENTOS:
            time.sleep(CSFLOAT_BACKOFF_BASE * (2 ** (intento - 1)))

    return None


def precio_csfloat(nombre: str) -> float | None:
    """
    Precio (EUR) del listing 'buy_now' más barato en CSFloat para
    `nombre`. Devuelve None si la fuente está desactivada, si el ítem
    no tiene listings activos, o si todos los reintentos fallan —
    nunca lanza una excepción hacia el llamador (FASE 9).

    Cachea por nombre normalizado durante toda la ejecución: si varias
    filas de Notion comparten la misma skin, se hace 1 sola consulta
    (FASE 6). El espaciado (CSFLOAT_REQUEST_DELAY) solo se aplica
    cuando se hace una llamada de red real, nunca en un cache hit.
    """
    if not _csfloat_activo:
        return None

    clave = normalizar_nombre(nombre)
    if clave in _csfloat_cache:
        print("    🗂  CSFloat cache hit")
        return _csfloat_cache[clave]

    print("    🗂  CSFloat cache miss")
    resultado = _consultar_csfloat(clave)
    _csfloat_cache[clave] = resultado
    time.sleep(CSFLOAT_REQUEST_DELAY)
    return resultado


# ─────────────────────────────────────────────────────────────────────
#  AGREGADOR  ─  mejor precio de todas las fuentes
# ─────────────────────────────────────────────────────────────────────

def obtener_precios(nombre: str) -> dict:
    ps = precio_steam(nombre)
    pp = precio_skinport(nombre)
    pc = precio_csfloat(nombre)
    fuentes = {
        k: v for k, v in {"Steam": ps, "Skinport": pp, "CSFloat": pc}.items()
        if v is not None
    }
    if not fuentes:
        return {"steam": None, "skinport": None, "csfloat": None, "mejor": None, "fuente": None}
    mejor = min(fuentes, key=fuentes.__getitem__)
    return {
        "steam": ps, "skinport": pp, "csfloat": pc,
        "mejor": fuentes[mejor], "fuente": mejor,
    }


# ─────────────────────────────────────────────────────────────────────
#  DISCORD
# ─────────────────────────────────────────────────────────────────────

_C = {"verde": 0x2ECC71, "rojo": 0xE74C3C, "naranja": 0xE67E22, "gold": 0xF0B429}


def _discord(payload: dict):
    if not DISCORD_WEBHOOK:
        return
    try:
        SESSION.post(DISCORD_WEBHOOK, json=payload, timeout=8)
    except Exception:
        pass


def _embed(title: str, desc: str, color: int, fields: list) -> dict:
    return {
        "embeds": [{
            "title":       title,
            "description": desc,
            "color":       color,
            "fields":      fields,
            "footer":      {"text": "CS2 Notion Updater · v3.2"},
            "timestamp":   _iso(),
        }]
    }


def discord_roi(nombre: str, roi: float, precio: float, inversion: float):
    _discord(_embed(
        f"{'🟢' if roi >= 0 else '🔴'}  ROI Alert — {nombre}",
        f"El ROI ha superado el umbral de **{ROI_ALERT_THRESHOLD}%**.",
        _C["gold"], [
            {"name": "💶 Precio actual",   "value": f"`{precio:.2f}€`",    "inline": True},
            {"name": "💸 Inversión total", "value": f"`{inversion:.2f}€`", "inline": True},
            {"name": "📈 ROI",             "value": f"**{roi:+.1f}%**",    "inline": True},
        ],
    ))


def discord_cambio(nombre: str, antes: float, ahora_p: float, pct: float):
    up = pct > 0
    _discord(_embed(
        f"{'📈' if up else '📉'}  Cambio de precio — {nombre}",
        f"Variación superior al **{PRICE_CHANGE_THRESHOLD}%** en 24h.",
        _C["verde"] if up else _C["rojo"], [
            {"name": "Precio anterior", "value": f"`{antes:.2f}€`",   "inline": True},
            {"name": "Precio actual",   "value": f"`{ahora_p:.2f}€`", "inline": True},
            {"name": "Variación",       "value": f"**{pct:+.1f}%**",  "inline": True},
        ],
    ))


def discord_resumen(stats: dict, valor: float, secs: float):
    rate  = stats["ok"] / max(stats["total"], 1) * 100
    color = _C["verde"] if rate >= 80 else _C["naranja"] if rate >= 50 else _C["rojo"]
    _discord(_embed(
        "📊  CS2 Notion Updater — Resumen", "",
        color, [
            {"name": "✅ Actualizadas",  "value": str(stats["ok"]),         "inline": True},
            {"name": "⚠️ Sin precio",    "value": str(stats["sin_precio"]), "inline": True},
            {"name": "❌ Errores",       "value": str(stats["error"]),      "inline": True},
            {"name": "📦 Total",         "value": str(stats["total"]),      "inline": True},
            {"name": "💰 Valor cartera", "value": f"{valor:.2f}€",          "inline": True},
            {"name": "⏱ Duración",      "value": f"{secs:.0f}s",           "inline": True},
        ],
    ))


# ─────────────────────────────────────────────────────────────────────
#  HISTORIAL LOCAL  (SQLite)
# ─────────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS historial (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha            TEXT NOT NULL,
                nombre_skin      TEXT NOT NULL,
                precio_steam     REAL,
                precio_skinport  REAL,
                precio_csfloat   REAL,
                precio_mejor     REAL,
                fuente           TEXT,
                roi_pct          REAL,
                cambio_24h_pct   REAL
            );
            CREATE INDEX IF NOT EXISTS idx_historial
                ON historial(nombre_skin, fecha);

            CREATE TABLE IF NOT EXISTS alertas (
                nombre_skin  TEXT,
                tipo         TEXT,
                fecha        TEXT,
                PRIMARY KEY (nombre_skin, tipo)
            );
        """)
        # Migración: bases de datos creadas con v3.1 no tienen esta
        # columna todavía. ALTER TABLE ADD COLUMN es seguro y no toca
        # los datos existentes.
        cols = {row[1] for row in con.execute("PRAGMA table_info(historial)")}
        if "precio_csfloat" not in cols:
            con.execute("ALTER TABLE historial ADD COLUMN precio_csfloat REAL")
            print("  🔧  Migración SQLite: columna 'precio_csfloat' añadida a historial")


def guardar_historial(
    nombre: str, precios: dict, roi: float | None, cambio: float | None
):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """INSERT INTO historial
               (fecha, nombre_skin, precio_steam, precio_skinport,
                precio_csfloat, precio_mejor, fuente, roi_pct, cambio_24h_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(), nombre,
                precios.get("steam"), precios.get("skinport"),
                precios.get("csfloat"),
                precios.get("mejor"), precios.get("fuente"),
                roi, cambio,
            ),
        )


def precio_hace_24h(nombre: str) -> float | None:
    umbral = (datetime.now() - timedelta(hours=24)).isoformat()
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            """SELECT precio_mejor FROM historial
               WHERE nombre_skin = ? AND fecha <= ?
               ORDER BY fecha DESC LIMIT 1""",
            (nombre, umbral),
        ).fetchone()
    return row[0] if row else None


def alerta_en_cooldown(nombre: str, tipo: str) -> bool:
    umbral = (datetime.now() - timedelta(hours=ALERT_COOLDOWN_HORAS)).isoformat()
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute(
            "SELECT 1 FROM alertas WHERE nombre_skin=? AND tipo=? AND fecha>?",
            (nombre, tipo, umbral),
        ).fetchone()
    return row is not None


def registrar_alerta(nombre: str, tipo: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO alertas (nombre_skin, tipo, fecha) VALUES(?,?,?)",
            (nombre, tipo, datetime.now().isoformat()),
        )


# ─────────────────────────────────────────────────────────────────────
#  NOTION HELPERS
# ─────────────────────────────────────────────────────────────────────

def setup_columnas():
    """Crea las columnas extra en Notion si no existen todavía."""
    print("  🔧  Columnas Notion…", end="", flush=True)
    try:
        existentes = set(notion_get_db()["properties"].keys())
        deseadas = {
            "Precio Steam":          {"number": {"format": "euro"}},
            "Precio Skinport":       {"number": {"format": "euro"}},
            "Precio CSFloat":        {"number": {"format": "euro"}},
            "% Cambio 24h":          {"number": {"format": "number"}},
            "Última Actualización":  {"date":   {}},
            "Fuente Precio":         {"select": {}},
            "Imagen":                {"url":    {}},
        }
        nuevas = {k: v for k, v in deseadas.items() if k not in existentes}
        if nuevas:
            notion_update_db(nuevas)
            print(f"  ✅  {len(nuevas)} nuevas → {', '.join(nuevas)}")
        else:
            print("  ✅  todas ya existen")
    except Exception as e:
        print(f"  ⚠  {e}")


def todas_las_paginas() -> list:
    """Paginación automática — devuelve TODOS los ítems."""
    pages, cursor = [], None
    while True:
        data = notion_query_db(cursor)
        pages.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return pages


def leer(page: dict, campo: str, tipo: str = "number"):
    """Lee una propiedad de Notion de forma segura."""
    try:
        prop = page["properties"][campo]
        if tipo == "number":
            return prop.get("number")
        if tipo in ("title", "rich_text"):
            items = prop.get(prop["type"], [])
            return items[0]["text"]["content"] if items else None
        if tipo == "url":
            return prop.get("url")
    except (KeyError, IndexError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────

def main():
    t0    = time.time()
    ahora = datetime.now()

    print("\n" + sep())
    print(f"  CS2 Notion Price Updater  v3.2  ·  {ahora:%d/%m/%Y %H:%M:%S}")
    print(f"  Fuentes  :  Steam Market  +  Skinport  +  CSFloat")
    print(f"  Notion   :  REST API directa (sin notion-client)")
    print(f"  CSFloat  :  {'✅  activo' if _csfloat_activo else '⚠️  desactivado (sin API key)'}")
    print(f"  Discord  :  {'✅  activo' if DISCORD_WEBHOOK else '❌  no configurado'}")
    print(f"  Historial:  {DB_PATH.name}")
    if DRY_RUN:
        print("  *** DRY-RUN — no se escribe nada en Notion ***")
    print(sep() + "\n")

    # ── Validación ────────────────────────────────────────────────────
    validar_configuracion()

    # ── Setup ─────────────────────────────────────────────────────────
    init_db()

    if SETUP_COLUMNS and not DRY_RUN:
        setup_columnas()

    cargar_skinport()
    print()

    # ── Notion ────────────────────────────────────────────────────────
    print("📡  Conectando con Notion…")
    try:
        paginas = todas_las_paginas()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            print("❌  Token inválido o sin permisos. Comprueba NOTION_TOKEN.")
        elif e.response is not None and e.response.status_code == 404:
            print("❌  Base de datos no encontrada. Comprueba DATABASE_ID y que la integración tenga acceso.")
        else:
            print(f"❌  Error Notion HTTP {e.response.status_code if e.response else '?'}: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌  Error inesperado: {e}")
        sys.exit(1)

    total = len(paginas)
    print(f"✅  {total} ítems encontrados.\n")

    stats         = {"ok": 0, "sin_precio": 0, "error": 0, "skip": 0, "total": total}
    valor_cartera = 0.0

    for i, page in enumerate(paginas, 1):
        nombre       = leer(page, "Nombre Skin", "title")
        coste_compra = float(leer(page, "Coste Compra") or 0)
        qty          = float(leer(page, "Qty")          or 1)
        img_actual   = leer(page, "Imagen", "url") if FETCH_IMAGES else None

        if not nombre:
            print(f"[{i:>3}/{total}]  (entrada sin nombre — omitida)")
            stats["skip"] += 1
            continue

        print(f"[{i:>3}/{total}]  {nombre}")
        precios = obtener_precios(nombre)

        if precios["mejor"] is None:
            print(f"         ⚠  Sin precio en ninguna fuente")
            stats["sin_precio"] += 1
            guardar_historial(nombre, precios, None, None)
            if i < total:
                time.sleep(REQUEST_DELAY)
            continue

        precio_nuevo   = precios["mejor"]
        valor_cartera += precio_nuevo * qty

        # ── ROI ───────────────────────────────────────────────────────
        inversion = (coste_compra * qty) if COSTE_POR_UNIDAD else coste_compra
        roi_pct   = (
            (precio_nuevo * qty - inversion) / inversion * 100
            if inversion > 0 else None
        )

        # ── Cambio 24h ────────────────────────────────────────────────
        precio_ayer = precio_hace_24h(nombre)
        cambio_24h  = (
            (precio_nuevo - precio_ayer) / precio_ayer * 100
            if precio_ayer else None
        )

        # ── Log ───────────────────────────────────────────────────────
        fuente_tag = f" [{precios['fuente']}]" if precios["fuente"] else ""
        cambio_str = f"  ({cambio_24h:+.1f}% 24h)" if cambio_24h is not None else ""
        roi_str    = f"  ROI: {roi_pct:+.1f}%"     if roi_pct    is not None else ""
        print(f"         💶  {precio_nuevo:.2f}€{fuente_tag}{cambio_str}{roi_str}", end="")
        detalles = [
            f"{etiqueta}: {valor:.2f}€"
            for etiqueta, valor in (
                ("Steam", precios["steam"]),
                ("Skinport", precios["skinport"]),
                ("CSFloat", precios["csfloat"]),
            )
            if valor is not None
        ]
        if len(detalles) > 1:
            print(f"\n               ↳ " + "  |  ".join(detalles), end="")

        # ── Imagen ────────────────────────────────────────────────────
        img_url = img_actual
        if FETCH_IMAGES and not img_url:
            img_url = imagen_steam(nombre)
            time.sleep(0.5)

        # ── Escribir en Notion ────────────────────────────────────────
        if not DRY_RUN:
            try:
                props: dict = {
                    "Precio Actual":        {"number": precio_nuevo},
                    "Última Actualización": {"date":   {"start": ahora.isoformat()}},
                }
                if precios["steam"]    is not None:
                    props["Precio Steam"]    = {"number": precios["steam"]}
                if precios["skinport"] is not None:
                    props["Precio Skinport"] = {"number": precios["skinport"]}
                if precios["csfloat"]  is not None:
                    props["Precio CSFloat"]  = {"number": precios["csfloat"]}
                if cambio_24h is not None:
                    props["% Cambio 24h"]    = {"number": round(cambio_24h, 2)}
                if precios["fuente"]:
                    props["Fuente Precio"]   = {"select": {"name": precios["fuente"]}}
                if img_url:
                    props["Imagen"]          = {"url": img_url}

                print("  ⏳ Actualizando Notion…", end="")
                notion_update_page(page["id"], props)
                print(" ✅")
                stats["ok"] += 1

            except Exception as e:
                print(f"\n         ❌  Notion error: {e}")
                stats["error"] += 1
        else:
            print("  (dry-run)")
            stats["ok"] += 1

        # ── Historial ─────────────────────────────────────────────────
        guardar_historial(nombre, precios, roi_pct, cambio_24h)

        # ── Alertas Discord ───────────────────────────────────────────
        if roi_pct is not None and roi_pct >= ROI_ALERT_THRESHOLD:
            if not alerta_en_cooldown(nombre, "roi"):
                discord_roi(nombre, roi_pct, precio_nuevo, inversion)
                registrar_alerta(nombre, "roi")

        if cambio_24h is not None and abs(cambio_24h) >= PRICE_CHANGE_THRESHOLD and precio_ayer:
            tipo = "subida" if cambio_24h > 0 else "bajada"
            if not alerta_en_cooldown(nombre, tipo):
                discord_cambio(nombre, precio_ayer, precio_nuevo, cambio_24h)
                registrar_alerta(nombre, tipo)

        if i < total:
            time.sleep(REQUEST_DELAY)

    # ── Resumen ───────────────────────────────────────────────────────
    secs = time.time() - t0
    print(f"\n{sep()}")
    print(f"  ✅  Actualizadas   : {stats['ok']}")
    print(f"  ⚠   Sin precio    : {stats['sin_precio']}")
    print(f"  ❌  Errores        : {stats['error']}")
    print(f"  ⏭   Omitidas      : {stats['skip']}")
    print(f"  📊  Total          : {stats['total']}")
    print(f"  💰  Valor cartera  : {valor_cartera:.2f}€")
    print(f"  ⏱   Duración       : {secs:.1f}s")
    print(f"  🕐  Fin             : {datetime.now():%H:%M:%S}")
    print(sep())

    discord_resumen(stats, valor_cartera, secs)


if __name__ == "__main__":
    main()
