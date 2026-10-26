import csv
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests.exceptions
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

import config
import engine
import hyperliquid_client as hl

load_dotenv()

_RUNTIME = Path(os.getenv("RUNTIME_DIR", "."))
_RUNTIME.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # la URL de cada request incluye el token de Telegram
log = logging.getLogger(__name__)
_fh = RotatingFileHandler(str(_RUNTIME / "copytrade.log"), maxBytes=5_000_000, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
log.addHandler(_fh)
log.propagate = False

MODO_FILE       = _RUNTIME / "mode.json"
LEADER_FILE     = _RUNTIME / "leader.json"
META_FILE       = _RUNTIME / "mirror_meta.json"
BASELINE_FILE   = _RUNTIME / "leader_baseline.json"
CLOSED_FILE     = _RUNTIME / "closed_mirrors.csv"
CLOSED_COLS     = ["timestamp_apertura", "timestamp_cierre", "coin", "lado", "leverage",
                   "notional_usdc", "leader_entry_px", "pnl_usdc_aprox", "motivo"]
ART = timezone(timedelta(hours=-3))

señales_pendientes: dict[str, dict] = {}  # id -> accion "abrir", para confirmacion en modo semi


# ── Persistencia (mismo patron que cryptobot/bot.py: tmp -> rename atomico) ──

def _leer_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def _escribir_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.rename(path)


def cargar_modo() -> str:
    return _leer_json(MODO_FILE, {}).get("modo", "semi")


def guardar_modo(modo: str):
    _escribir_json(MODO_FILE, {"modo": modo})


def cargar_leader() -> str:
    return _leer_json(LEADER_FILE, {}).get("address", config.LEADER_ADDRESS)


def guardar_leader(address: str):
    _escribir_json(LEADER_FILE, {"address": address})


def cargar_meta() -> dict:
    return _leer_json(META_FILE, {})


def guardar_meta(meta: dict):
    _escribir_json(META_FILE, meta)


def registrar_cierre(fila: dict):
    existe = CLOSED_FILE.exists()
    with open(CLOSED_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CLOSED_COLS, extrasaction="ignore")
        if not existe:
            writer.writeheader()
        writer.writerow(fila)


def cargar_baseline() -> dict:
    """Coins que el leader YA tenia abiertas cuando se lo empezo a seguir —
    se excluyen del mirror mientras sigan abiertas (no se mirrorea un portfolio
    preexistente). Cuando el leader cierra una, sale del baseline y su proxima
    apertura en esa coin ya se copia (ver liberar_baseline)."""
    return _leer_json(BASELINE_FILE, {"leader": "", "coins": []})


def fijar_baseline(leader_address: str, leader_pos: dict):
    _escribir_json(BASELINE_FILE, {"leader": leader_address, "coins": list(leader_pos.keys())})


_baseline_ausente: dict[str, int] = {}  # coin -> ciclos seguidos sin verla en el leader
CICLOS_PARA_LIBERAR = 3


def liberar_baseline(baseline: dict, leader_pos: dict) -> set:
    """Saca del baseline las coins que el leader ya cerro, y persiste el cambio.
    Exige CICLOS_PARA_LIBERAR ciclos seguidos sin la coin: si la API devuelve vacio
    por un error transitorio, no se libera todo el portfolio de golpe.
    Devuelve las coins que siguen excluidas."""
    coins = set(baseline.get("coins", []))
    for coin in coins:
        _baseline_ausente[coin] = 0 if coin in leader_pos else _baseline_ausente.get(coin, 0) + 1
    cerradas = {c for c in coins if _baseline_ausente[c] >= CICLOS_PARA_LIBERAR}
    if cerradas:
        for c in cerradas:
            _baseline_ausente.pop(c, None)
        coins -= cerradas
        _escribir_json(BASELINE_FILE, {"leader": baseline["leader"], "coins": sorted(coins)})
        log.info(f"Baseline: el leader cerro {', '.join(sorted(cerradas))} — desde ahora se copian")
    return coins


def con_reintentos(fn, *args, intentos: int = 3, espera: int = 2, **kwargs):
    for i in range(intentos):
        try:
            return fn(*args, **kwargs)
        except requests.exceptions.RequestException:
            if i == intentos - 1:
                raise
            time.sleep(espera * (i + 1))


def _entorno_label() -> str:
    return "TESTNET 🟡" if config.USE_TESTNET else "MAINNET 🔴"


def _modo_label(modo: str) -> str:
    return {"auto": "⚡ AUTO", "semi": "🔁 SEMI", "pausado": "⏸ PAUSADO"}.get(modo, modo)


# ── Ejecucion ─────────────────────────────────────────────────────────────────

async def _abrir_mirror(exchange, bot, chat_id: str, accion: dict, meta: dict):
    coin = accion["coin"]
    px_actual = None
    try:
        info = hl.crear_info()
        mids = info.all_mids()
        px_actual = float(mids.get(coin, accion["leader_entry_px"]))
    except Exception:
        px_actual = accion["leader_entry_px"]

    sz = round(accion["notional_usdc"] / px_actual, 5) if px_actual else 0
    if sz <= 0:
        log.warning(f"Tamaño calculado invalido para {coin}, se omite apertura")
        return

    try:
        con_reintentos(exchange.update_leverage, accion["leverage"], coin, True)
    except Exception as e:
        log.warning(f"No se pudo fijar leverage {accion['leverage']}x para {coin}: {e}")

    resultado = con_reintentos(
        exchange.market_open, coin, accion["is_buy"], sz, None, config.SLIPPAGE,
    )

    meta[coin] = {
        "abierto_ts":      datetime.now(ART).isoformat(),
        "leader_entry_px": accion["leader_entry_px"],
        "leader_leverage": accion["leader_leverage"],
        "nuestro_leverage": accion["leverage"],
        "notional_usdc":   accion["notional_usdc"],
    }
    guardar_meta(meta)

    lado = "LONG" if accion["is_buy"] else "SHORT"
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"✅ *Mirror abierto — {coin} {lado}*\n\n"
            f"Leverage  : `{accion['leverage']}x` (leader: `{accion['leader_leverage']}x`)\n"
            f"Notional  : `${accion['notional_usdc']:.2f} USDC`\n"
            f"Tamaño    : `{sz} {coin}`\n"
            f"_Hora_    : `{datetime.now(ART).strftime('%d/%m %H:%M')} ART`"
        ),
        parse_mode="Markdown",
    )
    log.info(f"Mirror abierto: {coin} {lado} notional=${accion['notional_usdc']:.2f} lev={accion['leverage']}x")


async def _cerrar_mirror(exchange, bot, chat_id: str, cierre: dict, meta: dict, posicion_previa: dict | None = None):
    coin = cierre["coin"]
    con_reintentos(exchange.market_close, coin)
    info_apertura = meta.pop(coin, {})
    guardar_meta(meta)

    if posicion_previa:
        registrar_cierre({
            "timestamp_apertura": info_apertura.get("abierto_ts", ""),
            "timestamp_cierre":   datetime.now(ART).isoformat(),
            "coin":               coin,
            "lado":               "LONG" if posicion_previa.get("is_long") else "SHORT",
            "leverage":           info_apertura.get("nuestro_leverage", posicion_previa.get("leverage")),
            "notional_usdc":      info_apertura.get("notional_usdc", posicion_previa.get("position_value")),
            "leader_entry_px":    info_apertura.get("leader_entry_px", ""),
            "pnl_usdc_aprox":     posicion_previa.get("unrealized_pnl", 0),
            "motivo":             cierre.get("motivo", ""),
        })

    motivo = {"leader_cerro": "el leader cerró su posición",
              "stop_loss_propio": "stop-loss propio",
              "manual": "cierre manual"}.get(cierre.get("motivo"), cierre.get("motivo", ""))
    extra = f" ({cierre['perdida_pct']*100:.1f}% de pérdida)" if "perdida_pct" in cierre else ""
    await bot.send_message(
        chat_id=chat_id,
        text=f"🔒 *Mirror cerrado — {coin}*\nMotivo: {motivo}{extra}",
        parse_mode="Markdown",
    )
    log.info(f"Mirror cerrado: {coin} — motivo={cierre.get('motivo')}")


# ── Ciclo principal ───────────────────────────────────────────────────────────

async def ciclo(context: ContextTypes.DEFAULT_TYPE):
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    (_RUNTIME / "heartbeat").write_text(datetime.now(ART).isoformat())

    modo = context.bot_data.get("modo", "semi")
    leader_address = context.bot_data.get("leader_address", "")
    if not leader_address:
        return  # sin leader configurado, nada que hacer

    exchange = context.bot_data["exchange"]
    account_address = context.bot_data["account_address"]
    meta = cargar_meta()

    try:
        info_leader = hl.crear_info_leader()  # el leader SIEMPRE vive en mainnet
        info_propio = hl.crear_info()         # nuestra cuenta: testnet o mainnet segun config
        leader_pos = con_reintentos(hl.posiciones_de, info_leader, leader_address)
        nuestras_pos = con_reintentos(hl.posiciones_de, info_propio, account_address)
    except Exception as e:
        log.error(f"Error leyendo estado de Hyperliquid: {e}")
        return

    # Stop-loss propio — corre siempre salvo pausado
    if modo != "pausado":
        for cierre in engine.chequear_stop_loss(nuestras_pos):
            try:
                await _cerrar_mirror(exchange, context.bot, chat_id, cierre, meta, nuestras_pos.get(cierre["coin"]))
            except Exception as e:
                log.error(f"Error en stop-loss propio para {cierre['coin']}: {e}")
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error cerrando {cierre['coin']} por stop-loss: {e}")

    baseline = cargar_baseline()
    baseline_coins = liberar_baseline(baseline, leader_pos) if baseline.get("leader") == leader_address else set()
    acciones = engine.calcular_acciones(leader_pos, nuestras_pos, baseline_coins)

    # Cierres (porque el leader salió) — siempre automaticos, salvo pausado
    if modo != "pausado":
        for cierre in acciones["cerrar"]:
            if cierre["coin"] in meta or cierre["coin"] in nuestras_pos:
                try:
                    await _cerrar_mirror(exchange, context.bot, chat_id, cierre, meta, nuestras_pos.get(cierre["coin"]))
                except Exception as e:
                    log.error(f"Error cerrando mirror {cierre['coin']}: {e}")
                    await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error cerrando {cierre['coin']}: {e}")

    # Aperturas — dependen del modo
    for accion in acciones["abrir"]:
        if modo == "pausado":
            continue
        if modo == "auto":
            try:
                await _abrir_mirror(exchange, context.bot, chat_id, accion, meta)
            except Exception as e:
                log.error(f"Error abriendo mirror {accion['coin']}: {e}")
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error abriendo {accion['coin']}: {e}")
        elif modo == "semi":
            ya_pendiente = any(v["coin"] == accion["coin"] for v in señales_pendientes.values())
            if ya_pendiente:
                continue  # ya se le pidio confirmacion por este coin, no duplicar
            señal_id = f"{accion['coin']}_{int(time.time())}"
            señales_pendientes[señal_id] = {**accion, "_ts": time.time()}
            lado = "LONG" if accion["is_buy"] else "SHORT"
            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Ejecutar", callback_data=f"ejecutar:{señal_id}"),
                InlineKeyboardButton("❌ Ignorar",  callback_data=f"ignorar:{señal_id}"),
            ]])
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🔔 *El leader abrió {accion['coin']} {lado}*\n\n"
                    f"Leverage : `{accion['leverage']}x`\n"
                    f"Notional propuesto : `${accion['notional_usdc']:.2f} USDC`\n"
                    f"_Expira en 1 hora_"
                ),
                parse_mode="Markdown",
                reply_markup=teclado,
            )

    # Limpieza de señales pendientes vencidas (TTL 1h)
    vencidas = [k for k, v in señales_pendientes.items() if time.time() - v["_ts"] > 3600]
    for k in vencidas:
        señales_pendientes.pop(k, None)


# ── Comandos Telegram ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    modo = context.bot_data.get("modo", "semi")
    leader = context.bot_data.get("leader_address") or "_sin configurar — usá /leader <address>_"
    await update.message.reply_text(
        f"🤖 *CopyTrade activo — {_modo_label(modo)}*\n"
        f"Entorno: *{_entorno_label()}*\n"
        f"Leader: `{leader}`\n\n"
        "Comandos:\n"
        "/estado — posiciones mirror abiertas\n"
        "/leader <address> — cambiar el trader a copiar\n"
        "/auto — ejecutar mirrors sin confirmación\n"
        "/semi — pide confirmación antes de cada mirror\n"
        "/pausar — pausar todo el copy trading\n"
        "/cerrar — cierre manual de emergencia",
        parse_mode="Markdown",
    )


async def cmd_leader(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        actual = context.bot_data.get("leader_address") or "(sin configurar)"
        await update.message.reply_text(f"Leader actual: `{actual}`\n\nUso: `/leader 0x...`", parse_mode="Markdown")
        return
    address = context.args[0].strip()
    if not address.startswith("0x") or len(address) != 42:
        await update.message.reply_text("⚠️ Esa no parece una dirección válida (formato 0x... de 42 caracteres).")
        return

    try:
        info_leader = hl.crear_info_leader()
        leader_pos = con_reintentos(hl.posiciones_de, info_leader, address)
    except Exception as e:
        await update.message.reply_text(f"⚠️ No pude leer las posiciones de esa dirección, no se cambió el leader: {e}")
        return

    context.bot_data["leader_address"] = address
    guardar_leader(address)
    fijar_baseline(address, leader_pos)
    await update.message.reply_text(
        f"✅ Leader actualizado a `{address}`\n\n"
        f"Tiene {len(leader_pos)} posiciones abiertas ahora mismo — "
        f"*no se van a mirrorear* (son su portfolio preexistente). "
        f"Solo se copia lo que abra de acá en adelante.",
        parse_mode="Markdown",
    )
    log.info(f"Leader cambiado a {address} — baseline fijado con {len(leader_pos)} posiciones excluidas")


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data["modo"] = "auto"
    guardar_modo("auto")
    await update.message.reply_text("⚡ Modo *AUTO* — los mirrors se ejecutan sin confirmación.", parse_mode="Markdown")


async def cmd_semi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data["modo"] = "semi"
    guardar_modo("semi")
    await update.message.reply_text("🔁 Modo *SEMI* — vas a recibir botones Ejecutar/Ignorar por cada mirror.", parse_mode="Markdown")


async def cmd_pausar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data["modo"] = "pausado"
    guardar_modo("pausado")
    await update.message.reply_text("⏸ CopyTrade *pausado*. Las posiciones abiertas no se tocan hasta /auto, /semi o /cerrar.", parse_mode="Markdown")


async def cmd_estado(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        info = hl.crear_info()
        nuestras_pos = hl.posiciones_de(info, context.bot_data["account_address"])
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error consultando posiciones: {e}")
        return

    if not nuestras_pos:
        await update.message.reply_text("Sin posiciones mirror abiertas.")
        return

    lineas = []
    for coin, p in nuestras_pos.items():
        lado = "LONG" if p["is_long"] else "SHORT"
        lineas.append(
            f"*{coin}* — {lado} `{p['leverage']}x`\n"
            f"  Entrada: `${p['entry_px']:,.2f}`\n"
            f"  PnL no realizado: `${p['unrealized_pnl']:+.2f}`"
        )
    await update.message.reply_text("\n\n".join(lineas), parse_mode="Markdown")


async def cmd_cerrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        info = hl.crear_info()
        nuestras_pos = hl.posiciones_de(info, context.bot_data["account_address"])
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error consultando posiciones: {e}")
        return

    if not nuestras_pos:
        await update.message.reply_text("Sin posiciones mirror abiertas. Nada que cerrar.")
        return

    botones = [[InlineKeyboardButton(f"❌ Cerrar {coin}", callback_data=f"cerrar_manual:{coin}")]
               for coin in nuestras_pos]
    botones.append([InlineKeyboardButton("🚨 Cerrar TODO", callback_data="cerrar_manual:TODO")])
    await update.message.reply_text(
        "Elegí qué posición cerrar a mercado:",
        reply_markup=InlineKeyboardMarkup(botones),
    )


async def callback_botones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    accion, _, dato = query.data.partition(":")
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    exchange = context.bot_data["exchange"]
    meta = cargar_meta()

    if accion == "ejecutar":
        plan = señales_pendientes.pop(dato, None)
        if not plan:
            await query.edit_message_text("⌛ Esta señal ya expiró o fue procesada.")
            return
        await query.edit_message_text(f"Ejecutando mirror de {plan['coin']}...")
        try:
            await _abrir_mirror(exchange, context.bot, chat_id, plan, meta)
        except Exception as e:
            log.error(f"Error ejecutando mirror confirmado {plan['coin']}: {e}")
            await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error abriendo {plan['coin']}: {e}")

    elif accion == "ignorar":
        señales_pendientes.pop(dato, None)
        await query.edit_message_text("❌ Señal ignorada.")

    elif accion == "cerrar_manual":
        try:
            info = hl.crear_info()
            nuestras_pos = hl.posiciones_de(info, context.bot_data["account_address"])
        except Exception as e:
            await query.edit_message_text(f"⚠️ Error consultando posiciones: {e}")
            return
        coins = list(nuestras_pos.keys()) if dato == "TODO" else [dato]
        await query.edit_message_text(f"Cerrando {', '.join(coins)}...")
        for coin in coins:
            try:
                await _cerrar_mirror(exchange, context.bot, chat_id, {"coin": coin, "motivo": "manual"}, meta, nuestras_pos.get(coin))
            except Exception as e:
                log.error(f"Error en cierre manual {coin}: {e}")
                await context.bot.send_message(chat_id=chat_id, text=f"⚠️ Error cerrando {coin}: {e}")


# ── Arranque ──────────────────────────────────────────────────────────────────

def main():
    token = os.environ["TELEGRAM_TOKEN"]
    account_address, exchange = hl.crear_exchange()

    app = Application.builder().token(token).build()
    app.bot_data["modo"] = cargar_modo()
    app.bot_data["leader_address"] = cargar_leader()
    if app.bot_data["leader_address"]:
        guardar_leader(app.bot_data["leader_address"])  # asegura que leader.json exista (lo lee el dashboard)
    app.bot_data["account_address"] = account_address
    app.bot_data["exchange"] = exchange

    leader_address = app.bot_data["leader_address"]
    baseline = cargar_baseline()
    if leader_address and baseline.get("leader") != leader_address:
        try:
            info_leader = hl.crear_info_leader()
            leader_pos = con_reintentos(hl.posiciones_de, info_leader, leader_address)
            fijar_baseline(leader_address, leader_pos)
            log.info(f"Baseline fijado al arrancar para {leader_address}: {len(leader_pos)} posiciones excluidas")
        except Exception as e:
            log.error(f"No se pudo fijar baseline al arrancar: {e}")

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("leader", cmd_leader))
    app.add_handler(CommandHandler("auto",   cmd_auto))
    app.add_handler(CommandHandler("semi",   cmd_semi))
    app.add_handler(CommandHandler("pausar", cmd_pausar))
    app.add_handler(CommandHandler("cerrar", cmd_cerrar))
    app.add_handler(CallbackQueryHandler(callback_botones))

    app.job_queue.run_repeating(ciclo, interval=config.POLL_INTERVAL_SECONDS, first=10)

    log.info(f"CopyTrade arrancado — {_entorno_label()} — leader={app.bot_data['leader_address'] or '(sin configurar)'}")
    app.run_polling()


if __name__ == "__main__":
    main()
