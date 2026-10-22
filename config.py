import os

USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"

# Wallet del trader a copiar (direccion 0x...) — configurable, puede estar vacia
# al arrancar y setearse despues con /leader desde Telegram.
LEADER_ADDRESS = os.getenv("LEADER_ADDRESS", "").strip()

CAPITAL_USDC          = float(os.getenv("CAPITAL_USDC", "100.0"))
STOP_LOSS_PCT         = float(os.getenv("STOP_LOSS_PCT", "0.15"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Red de seguridad ante datos corruptos del leader — no limita su leverage normal.
MAX_LEVERAGE_HARD_CAP = int(os.getenv("MAX_LEVERAGE_HARD_CAP", "50"))

# Slippage maximo aceptado en market_open/market_close del SDK de Hyperliquid.
SLIPPAGE = float(os.getenv("SLIPPAGE", "0.01"))
