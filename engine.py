"""
Reconciliacion leader vs nuestra cuenta — v1: solo abrir/cerrar, sin resize parcial.

El tamano de cada posicion nueva se reparte proporcionalmente a como el leader
tiene distribuido su margen entre sus posiciones *visibles ahora* (no conocemos
su equity total, solo lo que tiene abierto en este momento).
"""
import config


def pesos_leader(leader_pos: dict) -> dict:
    total = sum(p["margin_used"] for p in leader_pos.values())
    if total <= 0:
        return {}
    return {coin: p["margin_used"] / total for coin, p in leader_pos.items()}


def calcular_acciones(leader_pos: dict, nuestras_pos: dict, baseline_coins: set | None = None) -> dict:
    baseline_coins = baseline_coins or set()
    pesos = pesos_leader(leader_pos)
    abrir, cerrar = [], []

    for coin, p in leader_pos.items():
        if coin in nuestras_pos:
            continue  # v1: ya mirroreada, no se resizea
        if coin in baseline_coins:
            continue  # portfolio preexistente del leader — no se mirrorea
        peso = pesos.get(coin, 0)
        if peso <= 0:
            continue
        margen_nuestro = config.CAPITAL_USDC * peso
        leverage = max(1, min(p["leverage"], config.MAX_LEVERAGE_HARD_CAP))
        notional_usdc = margen_nuestro * leverage
        abrir.append({
            "coin":            coin,
            "is_buy":          p["is_long"],
            "leverage":        leverage,
            "notional_usdc":   round(notional_usdc, 2),
            "leader_entry_px": p["entry_px"],
            "leader_leverage": p["leverage"],
        })

    for coin in nuestras_pos:
        if coin not in leader_pos:
            cerrar.append({"coin": coin, "motivo": "leader_cerro"})

    return {"abrir": abrir, "cerrar": cerrar}


def chequear_stop_loss(nuestras_pos: dict) -> list[dict]:
    """Posiciones propias cuyo PnL no realizado supera el stop-loss propio
    (independiente de lo que haga el leader — el leaderboard no expone su SL/TP)."""
    cerrar = []
    for coin, p in nuestras_pos.items():
        margen = p["margin_used"]
        if margen <= 0:
            continue
        perdida_pct = -p["unrealized_pnl"] / margen
        if perdida_pct >= config.STOP_LOSS_PCT:
            cerrar.append({"coin": coin, "motivo": "stop_loss_propio", "perdida_pct": round(perdida_pct, 4)})
    return cerrar
