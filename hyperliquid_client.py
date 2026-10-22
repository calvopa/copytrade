import os

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

import config


def base_url() -> str:
    """Red de NUESTRA cuenta — testnet o mainnet segun config."""
    return constants.TESTNET_API_URL if config.USE_TESTNET else constants.MAINNET_API_URL


def crear_info() -> Info:
    """Cliente de lectura para NUESTRA cuenta (testnet o mainnet segun config)."""
    return Info(base_url(), skip_ws=True)


def crear_info_leader() -> Info:
    """El leader a copiar vive en el leaderboard real — siempre MAINNET,
    sin importar en que red estemos operando nosotros. Son dos redes
    completamente separadas (no es lo mismo que una testnet de Binance
    "espejando" el mismo mercado): la testnet de Hyperliquid no tiene un
    leaderboard de traders reales."""
    return Info(constants.MAINNET_API_URL, skip_ws=True)


def crear_exchange() -> tuple[str, Exchange]:
    """Agent wallet — puede operar pero no puede retirar fondos."""
    secret_key = os.environ["HYPERLIQUID_API_PRIVATE_KEY"]
    account_address = os.environ["HYPERLIQUID_ACCOUNT_ADDRESS"]
    wallet = Account.from_key(secret_key)
    exchange = Exchange(wallet, base_url(), account_address=account_address)
    return account_address, exchange


def posiciones_de(info: Info, address: str) -> dict:
    """Posiciones abiertas de una wallet (leader o propia) — {coin: {...}}."""
    if not address:
        return {}
    estado = info.user_state(address)
    out = {}
    for ap in estado.get("assetPositions", []):
        p = ap.get("position", {})
        coin = p.get("coin")
        if not coin:
            continue
        szi = float(p.get("szi", 0))
        if szi == 0:
            continue
        out[coin] = {
            "coin":            coin,
            "szi":             szi,
            "is_long":         szi > 0,
            "entry_px":        float(p.get("entryPx", 0)),
            "leverage":        int(p.get("leverage", {}).get("value", 1)),
            "margin_used":     float(p.get("marginUsed", 0)),
            "position_value":  float(p.get("positionValue", 0)),
            "unrealized_pnl":  float(p.get("unrealizedPnl", 0)),
        }
    return out
