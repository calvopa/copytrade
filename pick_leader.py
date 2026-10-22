#!/usr/bin/env python3
"""
Utilidad manual — imprime los top N traders del leaderboard publico de
Hyperliquid por ROI o PnL, para elegir un LEADER_ADDRESS.

NO corre en produccion ni dentro del bot — es solo para uso interactivo.

Uso:
    python3 pick_leader.py [ventana] [top_n] [orden] [min_cuenta]
    ventana:    day | week | month | allTime  (default: month)
    top_n:      cuantos mostrar (default: 15)
    orden:      roi | pnl  (default: roi)
    min_cuenta: filtra cuentas chicas que distorsionan el ranking por ROI%
                (default: 10000 USDC — una cuenta de $9 con +3.000.000% de ROI
                no es un trader a copiar, es ruido)
"""
import sys

import requests

URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


def _perf(row: dict, ventana: str) -> dict:
    for w, p in row.get("windowPerformances", []):
        if w == ventana:
            return p
    return {}


def main():
    ventana    = sys.argv[1] if len(sys.argv) > 1 else "month"
    top_n      = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    orden      = sys.argv[3] if len(sys.argv) > 3 else "roi"
    min_cuenta = float(sys.argv[4]) if len(sys.argv) > 4 else 10_000.0

    print("Descargando leaderboard de Hyperliquid (~40MB, puede tardar unos segundos)...")
    data = requests.get(URL, timeout=60).json()
    rows = data.get("leaderboardRows", [])
    print(f"{len(rows)} traders en el dataset.")

    candidatos = [r for r in rows if float(r.get("accountValue", 0) or 0) >= min_cuenta]
    print(f"{len(candidatos)} con cuenta >= ${min_cuenta:,.0f} (filtro para sacar ruido de cuentas chicas).\n")

    def clave(r):
        return float(_perf(r, ventana).get(orden, 0) or 0)

    ranked = sorted(candidatos, key=clave, reverse=True)

    print(f"Top {top_n} por {orden.upper()} — ventana '{ventana}':\n")
    for r in ranked[:top_n]:
        p = _perf(r, ventana)
        roi = float(p.get("roi", 0) or 0) * 100
        pnl = float(p.get("pnl", 0) or 0)
        cuenta = float(r.get("accountValue", 0) or 0)
        nombre = r.get("displayName") or "(sin nombre)"
        print(f"  {r['ethAddress']}  ROI {roi:7.2f}%  PnL ${pnl:>13,.0f}  "
              f"cuenta ${cuenta:>14,.0f}  {nombre}")

    print("\nCopiá la dirección (0x...) del trader elegido a LEADER_ADDRESS en .env,"
          " o usá /leader <address> por Telegram una vez que el bot esté corriendo.")


if __name__ == "__main__":
    main()
