"""
=============================================================
  GRID TRADING BOT - BINANCE
  Pares: BTC/USDT + ETH/USDT
  Autor: gerado via Claude
=============================================================

REQUISITOS:
  pip install python-binance python-dotenv

CONFIGURAÇÃO:
  Crie um arquivo .env na mesma pasta com:
    BINANCE_API_KEY=sua_chave_aqui
    BINANCE_API_SECRET=seu_secret_aqui

MODO TESTE:
  USE_TESTNET = True  → opera na testnet (sem dinheiro real)
  USE_TESTNET = False → opera real (CUIDADO!)

COMO FUNCIONA O GRID:
  1. Define um range de preço (ex: BTC ± 18% do preço atual)
  2. Divide esse range em N grids equidistantes
  3. Coloca ordens de compra abaixo do preço e venda acima
  4. Quando uma ordem é executada, coloca a ordem oposta no
     próximo nível — capturando o spread a cada oscilação
=============================================================
"""

import os
import time
import logging
import json
import threading
import urllib.request
import urllib.error
from datetime import datetime
from decimal import Decimal, ROUND_DOWN

from dotenv import load_dotenv
from binance.client import Client
from binance.exceptions import BinanceAPIException

# ─────────────────────────────────────────────
#  CARREGA VARIÁVEIS DE AMBIENTE
# ─────────────────────────────────────────────
load_dotenv()

API_KEY    = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_API_SECRET")

if not API_KEY or not API_SECRET:
    raise EnvironmentError(
        "❌  Defina BINANCE_API_KEY e BINANCE_API_SECRET no arquivo .env"
    )

# ─────────────────────────────────────────────
#  CONFIGURAÇÕES GERAIS
# ─────────────────────────────────────────────

USE_TESTNET = False          # ← Mude para False quando quiser operar real
CHECK_INTERVAL = 15 

# Capital por par em USDT (ajuste conforme seu saldo)
# R$1.200 ÷ 5,70 ≈ $210  |  R$800 ÷ 5,70 ≈ $140
GRID_CONFIGS = {
    "BTCUSDT": {
        "capital_usdt": 210.0,   # capital total alocado para BTC
        "num_grids":    20,       # mais grids → degraus menores → mais trades
        "range_pct":    0.05,     # range ± 5% (~.5k cada lado com BTC a 2k)
        "stop_loss_pct": 0.07,    # stop mais próximo do range
    },
    "ETHUSDT": {
        "capital_usdt": 140.0,
        "num_grids":    18,       # ETH oscilou ~8% na semana → mais grids
        "range_pct":    0.06,     # range ± 6%
        "stop_loss_pct": 0.08,
    },
}

LOG_FILE       = "grid_bot.log"
STATE_FILE     = "grid_state.json"   # salva estado entre reinicializações
CHECK_INTERVAL = 15                   # segundos entre cada verificação de ordens

# ─────────────────────────────────────────────
#  INTEGRAÇÃO COM O DASHBOARD
# ─────────────────────────────────────────────
# Preencha no .env ou diretamente aqui:
#   DASHBOARD_URL=https://seu-app.fly.dev
#   BOT_SECRET=grid-bot-secret
DASHBOARD_URL  = os.environ.get("DASHBOARD_URL", "").rstrip("/")
BOT_SECRET     = os.environ.get("BOT_SECRET", "grid-bot-secret")
STATS_INTERVAL = 30   # envia stats a cada N segundos

_start_time = time.time()

def _post_dashboard(endpoint: str, payload: dict):
    """Envia dados para o dashboard de forma silenciosa (não bloqueia o bot)."""
    if not DASHBOARD_URL:
        return
    try:
        data = json.dumps(payload).encode("utf-8")
        req  = urllib.request.Request(
            f"{DASHBOARD_URL}{endpoint}",
            data=data,
            headers={"Content-Type": "application/json", "x-bot-token": BOT_SECRET},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass   # nunca deixa o dashboard derrubar o bot


def send_log(level: str, message: str, symbol: str = None, meta: dict = None):
    """Envia um log para o dashboard em background."""
    if not DASHBOARD_URL:
        return
    payload = {
        "ts":      int(time.time() * 1000),
        "level":   level,
        "message": message,
    }
    if symbol: payload["symbol"] = symbol
    if meta:   payload["meta"]   = meta
    threading.Thread(target=_post_dashboard, args=("/api/bot/logs", payload), daemon=True).start()


def send_stats(bots: list):
    """Envia o status completo de todos os grids para o dashboard."""
    if not DASHBOARD_URL:
        return
    pairs = []
    for bot in bots:
        try:
            current = get_current_price(bot.client, bot.symbol)
        except Exception:
            current = bot.grid.get("entry_price", 0) if bot.grid else 0

        open_orders = list(bot.orders.values())
        # contar buys e sells pelo preço vs entry
        entry = bot.grid.get("entry_price", 0) if bot.grid else 0
        buys  = sum(1 for p in bot.orders.keys() if p < entry)
        sells = sum(1 for p in bot.orders.keys() if p > entry)

        pairs.append({
            "symbol":        bot.symbol,
            "pnl_usdt":      round(bot.pnl_usdt, 6),
            "trade_count":   bot.trade_count,
            "active":        bot.active,
            "entry_price":   bot.grid.get("entry_price", 0) if bot.grid else 0,
            "current_price":  current,
            "range_low":     bot.grid.get("lower_price", 0) if bot.grid else 0,
            "range_high":    bot.grid.get("upper_price", 0) if bot.grid else 0,
            "stop_loss":     bot.grid.get("stop_loss_price", 0) if bot.grid else 0,
            "buy_orders":    buys,
            "sell_orders":   sells,
        })

    payload = {
        "running":        True,
        "uptime_sec":     int(time.time() - _start_time),
        "pairs":          pairs,
        "total_pnl_usdt": round(sum(p["pnl_usdt"] for p in pairs), 6),
    }
    threading.Thread(target=_post_dashboard, args=("/api/bot/stats", payload), daemon=True).start()

# ─── DASHBOARD (opcional) ──────────────────────
# URL do seu dashboard no Fly.io + secret configurado na env BOT_REPORT_SECRET
DASHBOARD_URL    = os.environ.get("DASHBOARD_URL", "")     # ex: https://grid-dashboard.fly.dev
BOT_REPORT_SECRET = os.environ.get("BOT_REPORT_SECRET", "mude-esta-chave")
REPORT_INTERVAL  = 60   # envia report ao dashboard a cada 60s

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("grid_bot")

# ─────────────────────────────────────────────
#  CLIENTE BINANCE
# ─────────────────────────────────────────────

def create_client() -> Client:
    client = Client(API_KEY, API_SECRET, testnet=USE_TESTNET)
    try:
        client.ping()
        log.info("✅  Conexão com Binance OK  |  testnet=%s", USE_TESTNET)
        send_log("INFO", f"Conexão com Binance OK | testnet={USE_TESTNET}")
    except BinanceAPIException as e:
        log.error("❌  Falha na conexão: %s", e)
        raise
    return client


# ─────────────────────────────────────────────
#  UTILITÁRIOS DE PREÇO / QUANTIDADE
# ─────────────────────────────────────────────

def get_symbol_info(client: Client, symbol: str) -> dict:
    info = client.get_symbol_info(symbol)
    filters = {f["filterType"]: f for f in info["filters"]}

    tick_size  = Decimal(filters["PRICE_FILTER"]["tickSize"]).normalize()
    step_size  = Decimal(filters["LOT_SIZE"]["stepSize"]).normalize()
    min_qty    = Decimal(filters["LOT_SIZE"]["minQty"]).normalize()
    max_qty    = Decimal(filters["LOT_SIZE"]["maxQty"]).normalize()
    min_price  = Decimal(filters["PRICE_FILTER"]["minPrice"]).normalize()
    max_price  = Decimal(filters["PRICE_FILTER"]["maxPrice"]).normalize()
    min_notional = float(
        filters.get("MIN_NOTIONAL", {}).get("minNotional", 0)
        or filters.get("NOTIONAL", {}).get("minNotional", 10)
    )

    log.info(
        "ℹ️   %s filtros → tick=%.8f  step=%.8f  minQty=%.8f  minNotional=%.2f",
        symbol, tick_size, step_size, min_qty, min_notional,
    )

    return {
        "tick_size":     tick_size,
        "step_size":     step_size,
        "min_qty":       min_qty,
        "max_qty":       max_qty,
        "min_price":     min_price,
        "max_price":     max_price,
        "min_notional":  min_notional,
    }


def round_price(price: float, tick_size: Decimal) -> str:
    """Arredonda preço para o múltiplo mais próximo do tick_size (para baixo)."""
    tick = tick_size
    price_dec = Decimal(str(price))
    # Divide, trunca na casa inteira, multiplica de volta
    rounded = (price_dec / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    # Mantém mesmo número de casas decimais que tick_size
    return str(rounded.quantize(tick))


def round_qty(qty: float, step_size: Decimal) -> str:
    """Arredonda quantidade para o múltiplo mais próximo do step_size (para baixo)."""
    step = step_size
    qty_dec = Decimal(str(qty))
    rounded = (qty_dec / step).to_integral_value(rounding=ROUND_DOWN) * step
    return str(rounded.quantize(step))


def get_current_price(client: Client, symbol: str) -> float:
    ticker = client.get_symbol_ticker(symbol=symbol)
    return float(ticker["price"])


# ─────────────────────────────────────────────
#  CÁLCULO DOS NÍVEIS DE GRID
# ─────────────────────────────────────────────

def calculate_grid_levels(current_price: float, cfg: dict) -> dict:
    """
    Retorna dicionário com:
      lower_price  → preço mínimo do grid (stop loss automático abaixo daqui)
      upper_price  → preço máximo do grid
      levels       → lista de preços dos níveis do grid
      qty_per_grid → quantidade de crypto por nível
    """
    pct        = cfg["range_pct"]
    n          = cfg["num_grids"]
    capital    = cfg["capital_usdt"]

    lower = current_price * (1 - pct)
    upper = current_price * (1 + pct)
    step  = (upper - lower) / n

    levels = [lower + i * step for i in range(n + 1)]

    # Capital dividido igualmente por grid
    # Cada nível de compra usa (capital / num_grids) USDT
    usdt_per_grid = capital / n
    # Quantidade de crypto estimada ao preço do grid
    qty_per_grid  = usdt_per_grid / current_price   # valor médio

    return {
        "lower_price":    lower,
        "upper_price":    upper,
        "levels":         levels,
        "qty_per_grid":   qty_per_grid,
        "usdt_per_grid":  usdt_per_grid,
        "entry_price":    current_price,
        "stop_loss_price": current_price * (1 - cfg["stop_loss_pct"]),
    }


# ─────────────────────────────────────────────
#  COLOCAÇÃO DE ORDENS
# ─────────────────────────────────────────────

def place_limit_order(
    client: Client,
    symbol: str,
    side: str,          # "BUY" ou "SELL"
    price: float,
    qty: float,
    sym_info: dict,
) -> dict | None:
    """Coloca uma ordem limite. Retorna a ordem ou None em caso de erro."""
    p_str = round_price(price, sym_info["tick_size"])
    q_str = round_qty(qty,     sym_info["step_size"])
    p_dec = Decimal(p_str)
    q_dec = Decimal(q_str)

    # ── validações de filtro ──────────────────────────────────
    if q_dec <= Decimal("0") or q_dec < sym_info["min_qty"]:
        log.warning(
            "⚠️   Qty inválida  %s %s qty=%s  (min=%s) — ignorado",
            side, symbol, q_str, sym_info["min_qty"],
        )
        return None

    if p_dec < sym_info["min_price"] or (sym_info["max_price"] > 0 and p_dec > sym_info["max_price"]):
        log.warning(
            "⚠️   Preço fora do range  %s %s @ %s — ignorado",
            side, symbol, p_str,
        )
        return None

    notional = float(p_dec) * float(q_dec)
    if notional < sym_info["min_notional"]:
        log.warning(
            "⚠️   Notional baixo  %s %s  %.4f USDT  (min %.2f) — ignorado",
            side, symbol, notional, sym_info["min_notional"],
        )
        return None

    try:
        order = client.create_order(
            symbol=symbol,
            side=side,
            type=Client.ORDER_TYPE_LIMIT,
            timeInForce=Client.TIME_IN_FORCE_GTC,
            quantity=q_str,
            price=p_str,
        )
        log.info(
            "📋  Ordem %s  %s  qty=%s  @ %s USDT  (notional=%.2f)  id=%s",
            side, symbol, q_str, p_str, notional, order["orderId"],
        )
        return order
    except BinanceAPIException as e:
        log.error(
            "❌  Erro ao colocar ordem %s %s  qty=%s @ %s: %s",
            side, symbol, q_str, p_str, e,
        )
        return None


def cancel_all_open_orders(client: Client, symbol: str):
    try:
        orders = client.get_open_orders(symbol=symbol)
        for o in orders:
            client.cancel_order(symbol=symbol, orderId=o["orderId"])
            log.info("🗑️   Ordem cancelada  %s  id=%s", symbol, o["orderId"])
    except BinanceAPIException as e:
        log.error("❌  Erro ao cancelar ordens %s: %s", symbol, e)


# ─────────────────────────────────────────────
#  CLASSE PRINCIPAL DO GRID
# ─────────────────────────────────────────────

class GridBot:
    def __init__(self, client: Client, symbol: str, cfg: dict):
        self.client  = client
        self.symbol  = symbol
        self.cfg     = cfg
        self.sym_info = get_symbol_info(client, symbol)
        self.grid    : dict   = {}
        self.orders  : dict   = {}   # level_price → order_id
        self.active  : bool   = False
        self.pnl_usdt: float  = 0.0
        self.total_fees  : float = 0.0  # linha nova
        self.trade_count: int = 0

    # ── inicialização ──────────────────────────

    def start(self):
        log.info("=" * 55)
        log.info("🚀  Iniciando grid NEUTRO  %s", self.symbol)

        current_price = get_current_price(self.client, self.symbol)
        log.info("💰  Preço atual: %.4f USDT", current_price)

        self.grid = calculate_grid_levels(current_price, self.cfg)

        log.info(
            "📊  Range: %.4f – %.4f  |  %d grids  |  Stop Loss: %.4f",
            self.grid["lower_price"],
            self.grid["upper_price"],
            self.cfg["num_grids"],
            self.grid["stop_loss_price"],
        )

        # Cancela ordens abertas anteriores para o símbolo
        cancel_all_open_orders(self.client, self.symbol)

        # Compra metade do capital a mercado para grid neutro
        self._buy_initial_position(current_price)

        # Coloca ordens nos dois lados
        self._place_neutral_orders(current_price)
        self.active = True

    def _get_base_asset(self) -> str:
        """Retorna o asset base do par. Ex: BTCUSDT → BTC"""
        return self.symbol.replace("USDT", "")

    def _get_free_balance(self, asset: str) -> float:
        """Retorna saldo disponivel do asset na carteira Spot."""
        try:
            balance = self.client.get_asset_balance(asset=asset)
            return float(balance["free"]) if balance else 0.0
        except BinanceAPIException as e:
            log.error("❌  Erro ao buscar saldo %s: %s", asset, e)
            return 0.0

    def _buy_initial_position(self, current_price: float):
        """
        Compra metade do capital alocado a mercado SOMENTE se ainda nao tiver
        posicao suficiente em crypto.

        Logica:
          - Calcula quanto crypto equivale a metade do capital (posicao alvo)
          - Verifica saldo LIVRE de crypto na carteira
          - Se ja tiver >= 80% da posicao alvo → pula a compra (evita sobrecarga)
          - Se tiver entre 0% e 80% → compra apenas a diferenca
          - Se nao tiver nada → compra a posicao completa
        """
        base_asset   = self._get_base_asset()
        half_capital = self.cfg["capital_usdt"] / 2.0
        target_qty   = half_capital / current_price  # quantidade alvo de crypto

        # Saldo livre atual (exclui o que esta em ordens abertas)
        free_crypto  = self._get_free_balance(base_asset)
        free_usdt    = self._get_free_balance("USDT")

        free_value   = free_crypto * current_price  # valor em USD do crypto livre
        target_value = half_capital

        log.info(
            "🔍  Saldo livre  %s: %.6f (%s) = $%.2f  |  alvo: $%.2f",
            base_asset, free_crypto, base_asset, free_value, target_value,
        )

        # Ja tem >= 80% da posicao alvo → nao precisa comprar mais
        if free_value >= target_value * 0.80:
            log.info(
                "✅  Posicao ja suficiente  %s  (%.6f %s = $%.2f) — pulando compra inicial",
                self.symbol, free_crypto, base_asset, free_value,
            )
            return

        # Calcula quanto ainda falta comprar
        missing_value = target_value - free_value
        qty_to_buy    = missing_value / current_price

        # Verifica se tem USDT suficiente para a compra
        if free_usdt < missing_value * 0.5:
            log.warning(
                "⚠️   USDT insuficiente ($%.2f livre) para comprar $%.2f de %s — pulando",
                free_usdt, missing_value, base_asset,
            )
            return

        q_str = round_qty(qty_to_buy, self.sym_info["step_size"])
        q_dec = float(q_str)

        if q_dec < float(self.sym_info["min_qty"]):
            log.warning("⚠️   Qty inicial muito pequena para %s — pulando", self.symbol)
            return

        notional = current_price * q_dec
        if notional < self.sym_info["min_notional"]:
            log.warning("⚠️   Notional inicial baixo ($%.2f) para %s — pulando", notional, self.symbol)
            return

        log.info(
            "🛒  Comprando posicao complementar  %s  qty=%s  (~$%.2f) a mercado...",
            self.symbol, q_str, notional,
        )
        try:
            order = self.client.create_order(
                symbol=self.symbol,
                side="BUY",
                type=Client.ORDER_TYPE_MARKET,
                quantity=q_str,
            )
            filled_qty   = float(order["executedQty"])
            filled_price = float(order["cummulativeQuoteQty"]) / filled_qty if filled_qty > 0 else current_price
            log.info(
                "✅  Posicao complementar executada  %s  qty=%.6f  @ $%.4f  (total=$%.2f)",
                self.symbol, filled_qty, filled_price, filled_qty * filled_price,
            )
        except BinanceAPIException as e:
            log.error("❌  Erro ao comprar posicao inicial %s: %s", self.symbol, e)

    def _place_neutral_orders(self, current_price: float):
        """
        Grid neutro: coloca SELL acima e BUY abaixo do preço atual.

        Acima → vende o crypto que acabou de comprar (lucra na alta)
        Abaixo → compra mais com o USDT restante (lucra na baixa)
        """
        levels    = self.grid["levels"]
        qty       = self.grid["qty_per_grid"]
        buy_count = 0
        sell_count = 0

        for level in levels:
            if level < current_price * 0.9995:
                order = place_limit_order(
                    self.client, self.symbol, "BUY", level, qty, self.sym_info
                )
                if order:
                    self.orders[round(level, 8)] = order["orderId"]
                    buy_count += 1

            elif level > current_price * 1.0005:
                order = place_limit_order(
                    self.client, self.symbol, "SELL", level, qty, self.sym_info
                )
                if order:
                    self.orders[round(level, 8)] = order["orderId"]
                    sell_count += 1

        log.info(
            "✅  Grid neutro ativo  %s  →  %d BUY abaixo  +  %d SELL acima",
            self.symbol, buy_count, sell_count,
        )

    # ── loop principal ─────────────────────────

    def check_and_rebalance(self):
        """Verifica ordens executadas e recoloca no nível oposto."""
        if not self.active:
            return

        current_price = get_current_price(self.client, self.symbol)

        # ── STOP LOSS ──
        if current_price <= self.grid["stop_loss_price"]:
            log.warning(
                "🛑  STOP LOSS ativado!  %s  preço=%.4f  stop=%.4f",
                self.symbol, current_price, self.grid["stop_loss_price"],
            )
            send_log("WARNING", f"STOP LOSS ativado | preço=${current_price:,.2f} stop=${self.grid['stop_loss_price']:,.2f}", symbol=self.symbol)
            self._stop()
            return

        # ── Verifica ordens executadas ──
        try:
            open_orders = self.client.get_open_orders(symbol=self.symbol)
        except BinanceAPIException as e:
            log.error("❌  Erro ao buscar ordens abertas: %s", e)
            return

        open_ids = {o["orderId"] for o in open_orders}

        for level_price, order_id in list(self.orders.items()):
            if order_id not in open_ids:
                # Ordem foi executada → busca detalhes
                try:
                    order = self.client.get_order(
                        symbol=self.symbol, orderId=order_id
                    )
                except BinanceAPIException:
                    continue

                if order["status"] == "FILLED":
                    self._handle_filled(order, level_price, current_price)

    def _handle_filled(self, order: dict, level_price: float, current_price: float):
        """Processa uma ordem executada e coloca a ordem oposta."""
        side       = order["side"]
        fill_price = float(order["price"])
        qty        = float(order["executedQty"])
        levels     = self.grid["levels"]
        step       = levels[1] - levels[0]

        self.trade_count += 1

        if side == "BUY":
            sell_price    = fill_price + step
            gross         = (sell_price - fill_price) * qty
            fee           = (fill_price * qty * FEE_RATE) + (sell_price * qty * FEE_RATE)
            pnl_per_trade = gross - fee
            self.pnl_usdt   += pnl_per_trade
            self.total_fees  += fee
            log.info(
                "✅  COMPRA executada  %s  @ %.4f  qty=%.6f  → colocando VENDA @ %.4f  "
                "(lucro estimado $%.4f  taxa $%.4f)",
                self.symbol, fill_price, qty, sell_price, pnl_per_trade, fee,
            )
            new_order = place_limit_order(
                self.client, self.symbol, "SELL", sell_price, qty, self.sym_info
            )
        else:
            buy_price     = fill_price - step
            gross         = (fill_price - buy_price) * qty
            fee           = (fill_price * qty * FEE_RATE) + (buy_price * qty * FEE_RATE)
            pnl_per_trade = gross - fee
            self.pnl_usdt   += pnl_per_trade
            self.total_fees  += fee
            log.info(
                "✅  VENDA executada   %s  @ %.4f  qty=%.6f  → colocando COMPRA @ %.4f  "
                "(lucro estimado $%.4f  taxa $%.4f)",
                self.symbol, fill_price, qty, buy_price, pnl_per_trade, fee,
            )
            new_order = place_limit_order(
                self.client, self.symbol, "BUY", buy_price, qty, self.sym_info
            )

        if new_order:
            del self.orders[level_price]
            self.orders[round(float(new_order["price"]), 8)] = new_order["orderId"]

        log.info(
            "📈  PnL líquido acumulado  %s:  $%.4f  |  taxas pagas: $%.4f  (%d trades)",
            self.symbol, self.pnl_usdt, self.total_fees, self.trade_count,
        )

    # ── parada ────────────────────────────────

    def _stop(self):
        log.info("⏹️   Parando grid %s e cancelando todas as ordens...", self.symbol)
        cancel_all_open_orders(self.client, self.symbol)
        self.active = False

    def report(self) -> str:
        return (
            f"{self.symbol}  |  PnL: ${self.pnl_usdt:.4f}  "
            f"|  Trades: {self.trade_count}  "
            f"|  Ativo: {self.active}"
        )


# ─────────────────────────────────────────────
#  RELATÓRIO DIÁRIO
# ─────────────────────────────────────────────

def daily_report(bots: list[GridBot]):
    log.info("=" * 55)
    log.info("📊  RELATÓRIO  —  %s", datetime.now().strftime("%d/%m/%Y %H:%M"))
    total_pnl = 0.0
    for bot in bots:
        log.info("   %s", bot.report())
        total_pnl += bot.pnl_usdt
    log.info("   💵  PnL total estimado: $%.4f", total_pnl)
    log.info("=" * 55)


def send_dashboard_report(bots: list[GridBot], client, start_time: float):
    """Envia relatório ao dashboard via POST /api/report"""
    if not DASHBOARD_URL:
        return

    try:
        pairs_data = []
        for bot in bots:
            current_price = get_current_price(client, bot.symbol)
            open_orders   = client.get_open_orders(symbol=bot.symbol)
            buys  = sum(1 for o in open_orders if o["side"] == "BUY")
            sells = sum(1 for o in open_orders if o["side"] == "SELL")

            pairs_data.append({
                "symbol":           bot.symbol,
                "active":           bot.active,
                "entry_price":      bot.grid.get("entry_price", 0),
                "current_price":    current_price,
                "range_lower":      bot.grid.get("lower_price", 0),
                "range_upper":      bot.grid.get("upper_price", 0),
                "stop_loss_price":  bot.grid.get("stop_loss_price", 0),
                "pnl_usdt":         round(bot.pnl_usdt, 6),
                "trade_count":      bot.trade_count,
                "open_orders_buy":  buys,
                "open_orders_sell": sells,
                "grid_pct":         bot.cfg.get("range_pct", 0),
                "grids":            bot.cfg.get("num_grids", 0),
            })

        payload = json.dumps({
            "timestamp":       datetime.now().isoformat(),
            "uptime_seconds":  int(time.time() - start_time),
            "pairs":           pairs_data,
            "total_pnl_usdt":  round(sum(b.pnl_usdt for b in bots), 6),
            "total_fees":  round(bot.total_fees, 6),  # linha nova
            "total_trades":    sum(b.trade_count for b in bots),
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{DASHBOARD_URL}/api/report",
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "x-bot-secret":  BOT_REPORT_SECRET,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as res:
            if res.status == 200:
                log.info("📡  Report enviado ao dashboard OK")
    except Exception as e:
        log.warning("⚠️   Falha ao enviar report ao dashboard: %s", e)


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────

def main():
    log.info("🤖  Grid Bot iniciando...")
    log.info("⚠️   Modo: %s", "TESTNET" if USE_TESTNET else "🔴 REAL")

    client = create_client()

    # Cria um bot para cada par configurado
    bots: list[GridBot] = []
    for symbol, cfg in GRID_CONFIGS.items():
        bot = GridBot(client, symbol, cfg)
        bot.start()
        bots.append(bot)

    last_report    = time.time()
    last_dashboard = time.time()
    start_time     = time.time()
    report_interval = 3600  # relatório a cada 1 hora

    log.info("✅  Todos os grids ativos. Monitorando a cada %ds...", CHECK_INTERVAL)
    send_log("INFO", f"Bot iniciado | {len(bots)} grids ativos | testnet={USE_TESTNET}")
    send_stats(bots)
    if DASHBOARD_URL:
        log.info("📡  Dashboard: %s", DASHBOARD_URL)

    try:
        while any(b.active for b in bots):
            for bot in bots:
                if bot.active:
                    bot.check_and_rebalance()

            now = time.time()

            # Relatório no log a cada 1h
            if now - last_report >= report_interval:
                daily_report(bots)
                last_report = now

            # Envia ao dashboard a cada 60s
            if now - last_dashboard >= REPORT_INTERVAL:
                send_dashboard_report(bots, client, start_time)
                last_dashboard = now

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        log.info("⏹️   Interrompido pelo usuário.")
    finally:
        for bot in bots:
            bot._stop()
        daily_report(bots)
        log.info("👋  Bot encerrado.")


if __name__ == "__main__":
    main()