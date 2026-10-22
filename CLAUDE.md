# CLAUDE.md

Guía para Claude Code en este repo.

## Qué es esto

Bot de **copy trading** sobre **Hyperliquid** (exchange de perpetuos, no Binance): copia las
posiciones abiertas de un trader elegido (`LEADER_ADDRESS`) en vez de generar señales propias.

**Por qué Hyperliquid y no Binance**: el plan original era copiar un trader del leaderboard de
Binance Futures, pero el endpoint que exponía las posiciones de otro trader (`getOtherPosition`)
pasó de público a privado el 4/02/2024 (ahora requiere cookies de sesión logueada). Hyperliquid
en cambio expone esto de forma pública, gratis y oficial — ver "Fuentes de datos" abajo.

## Infraestructura

| Elemento | Valor |
|---|---|
| Código servidor | `/opt/stacks/copytrade/` en `192.168.232.60` (srv-hometainer) |
| Código local | `/home/calvop/copytrade/` — dev |
| Contenedor | `copytrade-bot` (un solo container, no hay dashboard propio en v1) |
| Volumen datos | `copytrade_data` montado en `/data` |
| Entorno | `USE_TESTNET=true` en `.env` del servidor |
| GitHub | `github.com/calvopa/copytrade` (privado) |

## Fuentes de datos — todas públicas y oficiales, sin scraping

- **Discovery** (elegir trader a copiar): `https://stats-data.hyperliquid.xyz/Mainnet/leaderboard`
  — dataset completo (~47k traders, ~40MB), gratis, sin auth. Usar `pick_leader.py` para explorarlo
  (no corre en producción, es una utilidad manual).
- **Tracking** (posiciones en vivo de cualquier wallet): `POST https://api.hyperliquid.xyz/info`
  con `{"type":"clearinghouseState","user":"<address>"}` — público, sin auth, cualquier dirección.
  Lo envuelve `hyperliquid_client.posiciones_de()`.
- **Ejecución**: SDK oficial `hyperliquid-python-sdk`, firma EIP-712 con una **agent wallet**
  (Settings → API en app.hyperliquid.xyz) — puede operar pero **no puede retirar fondos**.

## Comandos del día a día

```bash
# Logs en vivo
ssh pablo@192.168.232.60 "docker logs -f copytrade-bot"

# Estado del contenedor
ssh pablo@192.168.232.60 "docker ps --filter name=copytrade"

# Deploy — git push + pull + rebuild, NUNCA scp (no hay bind mount)
cd /home/calvop/copytrade && git push origin main
ssh pablo@192.168.232.60 "cd /opt/stacks/copytrade && git pull && docker compose up -d --build"

# Elegir un trader a copiar (local, no requiere credenciales)
python3 pick_leader.py month 15 roi 50000   # ventana / top_n / orden / cuenta_minima_usd
```

## Arquitectura

| Archivo | Rol |
|---|---|
| `config.py` | Parámetros vía `os.getenv()` |
| `hyperliquid_client.py` | Wrapper fino sobre `hyperliquid-python-sdk` (lectura `Info`, escritura `Exchange`) |
| `engine.py` | Reconciliación: compara posiciones del leader vs las nuestras, decide abrir/cerrar, y el stop-loss propio |
| `bot.py` | Telegram + loop de polling (`job_queue.run_repeating`) + ejecución |
| `pick_leader.py` | Utilidad manual para elegir `LEADER_ADDRESS` — no corre en producción |

### Sizing (v1 — sin resize parcial)

Cuando el leader abre una posición nueva en `coin`, el tamaño propio se calcula proporcional a
cómo el leader reparte su margen entre sus posiciones *visibles ahora* (no conocemos su equity
total): `nuestro_margen = CAPITAL_USDC * (marginUsed_leader_coin / marginUsed_total_leader)`.
El leverage replica el real del leader, capado por `MAX_LEVERAGE_HARD_CAP` (red de seguridad
ante datos corruptos, no un límite normal).

Si el leader resizea una posición que ya estamos espejando, **v1 no lo replica** — queda en
backlog. Si el leader la cierra del todo, cerramos la nuestra.

### Seguridad propia

El leaderboard no expone el SL/TP del leader, solo su posición — por eso hay un
`STOP_LOSS_PCT` propio (default 15%) que cierra una posición mirror si pierde más que eso,
independiente de lo que haga el leader. Corre en cada ciclo salvo en modo `pausado`.

### Modos de operación

- `"semi"` (default) — botones Ejecutar/Ignorar por Telegram antes de cada apertura; los cierres
  (leader salió, o stop-loss propio) son siempre automáticos
- `"auto"` — ejecuta aperturas sin confirmación
- `"pausado"` — no toca nada, ni aperturas ni cierres (ni siquiera el stop-loss propio)

Cambiar desde Telegram: `/auto`, `/semi`, `/pausar`. Cambiar el trader a copiar: `/leader <address>`.

## Gotcha importante: el leader siempre es MAINNET

Testnet y mainnet de Hyperliquid son redes completamente separadas — la testnet no tiene un
leaderboard de traders reales. Por eso `hyperliquid_client.py` tiene **dos** clientes de lectura
distintos: `crear_info_leader()` (siempre `MAINNET_API_URL`, para leer al trader que copiamos)
y `crear_info()` (testnet o mainnet según `USE_TESTNET`, para leer/operar nuestra propia cuenta).
Si alguna vez se lee al leader con el cliente equivocado, `posiciones_de()` devuelve `{}`
silenciosamente (no tira error) — se ve en el log como "el leader nunca tiene posiciones",
no como una excepción.

## Reglas críticas

- **Nunca** `USE_TESTNET=false` sin pedido explícito del usuario
- Deploy = `git push` + `git pull` + `docker compose up --build`. Nunca `scp`
- La agent wallet **no puede retirar fondos** — aun así, nunca commitear
  `HYPERLIQUID_API_PRIVATE_KEY` ni ponerlo en código, solo en `.env`
- **Gotcha de Hyperliquid**: el chain id de firma difiere entre testnet y mainnet para acciones
  firmadas por el usuario. Si se promueve a mainnet sin actualizar la config de firma, las
  órdenes fallan con `INVALID_SIGNATURE` — no es un bug del código, es la config de red.

## Prerequisito para correr esto

Crear una agent wallet en Hyperliquid Testnet (`app.hyperliquid-testnet.xyz` → Settings → API)
y cargarla con faucet USDC antes de poner `HYPERLIQUID_API_PRIVATE_KEY` /
`HYPERLIQUID_ACCOUNT_ADDRESS` en `.env`.

## Backlog

- [ ] Replicar resize parcial de posiciones (hoy v1 solo abre/cierra)
- [ ] Permitir más de un leader en simultáneo
- [ ] Integrar como "Bot 4" en el dashboard de `cryptobot` (mismo patrón que se usó para FreqBB)
- [ ] `pick_leader.py` podría cachear el dataset del leaderboard (40MB) en vez de descargarlo cada vez
