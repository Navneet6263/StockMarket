"""Telegram Signal Bot: Scanner -> Filter -> DB -> Telegram -> Tracker."""

from __future__ import annotations

import html
import os
import threading
import time
from datetime import date, datetime
from typing import Dict, List, Optional

import requests

from app.market_intelligence import MarketIntelligenceService
from app.signal_tracker import get_tracker
from app.stock_scanner import get_scanner

SCAN_INTERVAL = int(os.getenv("SIGNAL_INTERVAL_SEC", "60"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "3"))
MAX_VIX = float(os.getenv("MAX_VIX_THRESHOLD", "22"))
COOLDOWN_SEC = int(os.getenv("SIGNAL_COOLDOWN_SEC", "300"))
MIN_RR = float(os.getenv("MIN_RISK_REWARD", "2.0"))


def _telegram_config() -> Dict[str, str | bool]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return {
        "token": token,
        "chat_id": chat_id,
        "configured": bool(token and chat_id),
        "api": f"https://api.telegram.org/bot{token}" if token else "",
    }


class TelegramSignalBot:
    def __init__(self, db=None):
        self.scanner = get_scanner()
        self.tracker = get_tracker(db=db)
        self.market_intelligence = MarketIntelligenceService()
        self.db = db

        self.last_signal: Dict[str, str] = {}
        self.last_signal_time: Dict[str, float] = {}
        self.daily_trade_count = 0
        self.current_date: Optional[date] = None
        self.running = False
        self.no_trade_sent_today = False
        self._thread: Optional[threading.Thread] = None

        self.last_error = ""
        self.last_send_status: Dict = {"ok": False, "message": "No Telegram message sent yet."}
        self.last_scan_summary: Dict = {}

    def _send(self, text: str) -> bool:
        config = _telegram_config()
        if not config["configured"]:
            self.last_error = "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set."
            self.last_send_status = {"ok": False, "message": self.last_error}
            print("[TG] Token/ChatID not set")
            return False

        try:
            response = requests.post(
                f"{config['api']}/sendMessage",
                json={"chat_id": config["chat_id"], "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
            if response.status_code != 200:
                self.last_error = f"Send failed: {response.status_code} {response.text}"
                self.last_send_status = {"ok": False, "message": self.last_error}
                print(f"[TG] {self.last_error}")
                return False

            self.last_error = ""
            self.last_send_status = {
                "ok": True,
                "message": "Message sent",
                "sent_at": datetime.utcnow().isoformat(),
            }
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.last_send_status = {"ok": False, "message": str(exc)}
            print(f"[TG] Send error: {exc}")
            return False

    def _reset_daily(self):
        today = date.today()
        if self.current_date != today:
            self.daily_trade_count = 0
            self.current_date = today
            self.no_trade_sent_today = False

    def _is_duplicate(self, symbol: str, action: str) -> bool:
        if self.last_signal.get(symbol) == action:
            return (time.time() - self.last_signal_time.get(symbol, 0)) < COOLDOWN_SEC
        return False

    def _record(self, symbol: str, action: str):
        self.last_signal[symbol] = action
        self.last_signal_time[symbol] = time.time()
        self.daily_trade_count += 1

    def _gate_check(self, signal: Dict) -> bool:
        self._reset_daily()
        if self.daily_trade_count >= MAX_TRADES_PER_DAY:
            return False
        if signal.get("vix_value", 0) > MAX_VIX:
            return False
        if self._is_duplicate(signal["symbol"], signal["action"]):
            return False
        return True

    @staticmethod
    def _safe(value) -> str:
        if value is None:
            return ""
        return html.escape(str(value), quote=False)

    @staticmethod
    def _format_macro_block(macro_snapshot: Optional[Dict]) -> str:
        if not macro_snapshot:
            return ""

        lines = ["<b>Morning Macro Checklist</b>"]
        for item in macro_snapshot.get("checklist", []):
            if item.get("change_pct") is not None:
                delta = f"{item.get('change_pct', 0):+.2f}%"
            else:
                delta = f"{item.get('change_bps', 0):+.1f} bps"
            lines.append(
                f"- {TelegramSignalBot._safe(item.get('label'))}: {TelegramSignalBot._safe(item.get('value'))} ({delta}) | {TelegramSignalBot._safe(item.get('signal'))}"
            )
        lines.append(f"Macro bias: {TelegramSignalBot._safe(macro_snapshot.get('risk_mode', 'mixed').replace('_', ' '))}")
        return "\n".join(lines)

    @staticmethod
    def _format_news_block(news_feed: Optional[Dict], limit: int = 2) -> str:
        items = (news_feed or {}).get("items", [])[:limit]
        if not items:
            return ""

        lines = ["<b>Headline Watch</b>"]
        for item in items:
            published = item.get("published_at", "")
            short_time = published.replace("T", " ").replace("Z", "")[:16] if published else ""
            lines.append(
                f"- {TelegramSignalBot._safe(item.get('title'))} [{TelegramSignalBot._safe(item.get('publisher'))}, {TelegramSignalBot._safe(short_time)}]"
            )
        return "\n".join(lines)

    @staticmethod
    def _format_signal(signal: Dict, macro_snapshot: Optional[Dict] = None, news_feed: Optional[Dict] = None) -> str:
        reasons = signal.get("reasons", [])[:3]
        reason_lines = (
            "\n".join(f"- {TelegramSignalBot._safe(reason)}" for reason in reasons)
            if reasons
            else "- No extra reason available."
        )
        macro_block = TelegramSignalBot._format_macro_block(macro_snapshot)
        news_block = TelegramSignalBot._format_news_block(news_feed)
        win = round(signal.get("backtest_win_rate", 0) * 100, 1)
        pf = signal.get("backtest_pf", 0)
        dd = round(signal.get("backtest_max_dd", 0) * 100, 1)

        sections = [
            (
                f"<b>{TelegramSignalBot._safe(signal['symbol'])} | {TelegramSignalBot._safe(signal['action'])}</b>\n"
                f"Setup: {TelegramSignalBot._safe(signal.get('setup', ''))}\n"
                "------------------------------\n"
                f"Entry: <code>{signal['entry_price']:.2f}</code>\n"
                f"SL: <code>{signal['stop_loss']:.2f}</code>\n"
                f"T1: <code>{signal['target_1']:.2f}</code>\n"
                f"T2: <code>{signal['target_2']:.2f}</code>\n"
                "------------------------------\n"
                f"RR: 1:{signal.get('rr', 0)}\n"
                f"Confidence: {round(signal.get('confidence', 0) * 100, 1)}%\n"
                f"Regime: {TelegramSignalBot._safe(signal.get('regime', ''))}\n"
                f"MTF: {TelegramSignalBot._safe(signal.get('mtf_sync', ''))}\n"
                f"Volume: {signal.get('volume_ratio', 0)}x\n"
                f"Trade Quality: {TelegramSignalBot._safe(signal.get('quality_grade', 'A'))} ({signal.get('quality_score', 0)}/100)"
            ),
            f"<b>Why This Setup</b>\n{reason_lines}",
            (
                "<b>Backtest</b>\n"
                f"- Win Rate: {win}%\n"
                f"- Profit Factor: {pf}\n"
                f"- Max DD: {dd}%"
            ),
        ]

        if macro_block:
            sections.append(macro_block)
        if news_block:
            sections.append(news_block)

        return "\n\n".join(sections)

    @staticmethod
    def _format_no_trade(top_rejected: List[Dict], macro_snapshot: Optional[Dict] = None) -> str:
        lines = [
            "<b>NO TRADE ZONE</b>",
            "Market unclear. Quality filters are not aligned.",
            "------------------------------",
        ]
        for item in top_rejected[:8]:
            reasons = item.get("rejection_reasons", [])[:3]
            fallback = [
                f"Quality {TelegramSignalBot._safe(item.get('quality_grade', '?'))}",
                f"RR 1:{item.get('rr', 0)}",
            ]
            safe_reasons = [TelegramSignalBot._safe(reason) for reason in (reasons or fallback)]
            lines.append(f"- {TelegramSignalBot._safe(item['symbol'])}: {' | '.join(safe_reasons)}")

        if macro_snapshot:
            lines.extend(["------------------------------", TelegramSignalBot._format_macro_block(macro_snapshot)])
        lines.append("\n<i>Patience is edge. Wait for A-grade alignment.</i>")
        return "\n".join(lines)

    def _scan_and_send(self, stocks: Optional[List[str]] = None) -> List[Dict]:
        scan_result = self.scanner.scan_all(stocks)
        passed = scan_result.get("passed", [])
        top_rejected = scan_result.get("top_rejected", [])
        signals_sent: List[Dict] = []

        macro_snapshot = None
        try:
            macro_snapshot = self.market_intelligence.get_macro_snapshot()
        except Exception as exc:
            self.last_error = f"Macro snapshot unavailable: {exc}"

        for signal in passed:
            if not self._gate_check(signal):
                continue

            signal_id = self.tracker.save_signal(signal)
            if signal_id:
                signal["signal_id"] = signal_id

            news_feed = None
            try:
                news_feed = self.market_intelligence.get_news_feed(signal["symbol"], limit=2)
            except Exception:
                news_feed = None

            message = self._format_signal(signal, macro_snapshot=macro_snapshot, news_feed=news_feed)
            self._send(message)
            self._record(signal["symbol"], signal["action"])
            signals_sent.append(signal)

        no_trade_message_sent = False
        self._reset_daily()
        if not signals_sent and not self.no_trade_sent_today:
            self._send(self._format_no_trade(top_rejected, macro_snapshot=macro_snapshot))
            self.no_trade_sent_today = True
            no_trade_message_sent = True

        self.last_scan_summary = {
            "scanned_at": datetime.utcnow().isoformat(),
            "signals_sent": len(signals_sent),
            "signals": signals_sent[:5],
            "no_trade_message_sent": no_trade_message_sent,
            "top_rejected": top_rejected[:5],
            "discovery": scan_result.get("discovery", {}),
            "macro_snapshot": macro_snapshot or {},
            "scanner_stats": {
                "total_scanned": scan_result.get("total_scanned", 0),
                "successful_scans": scan_result.get("successful_scans", 0),
                "signals": scan_result.get("signals", 0),
                "rejected": scan_result.get("rejected", 0),
                "elapsed_sec": scan_result.get("elapsed_sec", 0),
            },
            "last_error": self.last_error,
        }
        return signals_sent

    def _track_loop(self):
        while self.running:
            try:
                open_signals = self.tracker.get_open_signals()
                for signal in open_signals:
                    event = self.tracker._check_signal(signal)
                    if event:
                        self.tracker._close_signal(event)
                        self._send_track_alert(event)
            except Exception as exc:
                print(f"[TRACKER] Error: {exc}")
            time.sleep(30)

    def _send_track_alert(self, event: Dict):
        status = event["status"]
        symbol = event["symbol"]
        pnl = event["pnl_pct"]

        if status == "T1_HIT":
            title, advice = "TARGET 1 HIT", "Book 50% profit. Trail SL to entry."
        elif status == "T2_HIT":
            title, advice = "TARGET 2 HIT - FULL EXIT", "Book full profit. Well played."
        else:
            title, advice = "STOP LOSS HIT", "Exit done. Capital preserved."

        pnl_label = "POSITIVE" if pnl > 0 else "NEGATIVE"
        message = (
            f"<b>{self._safe(symbol)} | {self._safe(title)}</b>\n"
            "------------------------------\n"
            f"Entry: <code>{event['entry_price']:.2f}</code>\n"
            f"Current: <code>{event['current_price']:.2f}</code>\n"
            "------------------------------\n"
            f"{pnl_label} P&L: {'+' if pnl > 0 else ''}{pnl:.2f}%\n\n"
            f"Advice: {self._safe(advice)}"
        )
        self._send(message)

    def _main_loop(self):
        macro_snapshot = None
        try:
            macro_snapshot = self.market_intelligence.get_macro_snapshot()
        except Exception:
            macro_snapshot = None

        start_message = (
            "<b>Signal Bot Started</b>\n\n"
            "Scanning live NSE screener universe continuously.\n"
            "------------------------------\n"
            "Filters:\n"
            "- Quality A only\n"
            "- MTF aligned\n"
            "- Trending regime\n"
            "- Volume + breakout\n"
            f"- RR >= 1:{MIN_RR}\n"
            f"- Max {MAX_TRADES_PER_DAY} trades/day\n"
            f"- VIX < {MAX_VIX}\n"
            "------------------------------\n"
            "Auto target and SL tracking is ON."
        )
        if macro_snapshot:
            start_message = f"{start_message}\n\n{self._format_macro_block(macro_snapshot)}"

        self._send(start_message)

        tracker_thread = threading.Thread(target=self._track_loop, daemon=True)
        tracker_thread.start()

        while self.running:
            try:
                self._scan_and_send()
            except Exception as exc:
                self.last_error = str(exc)
                print(f"[TG] Scan error: {exc}")
            time.sleep(SCAN_INTERVAL)

    def start(self):
        config = _telegram_config()
        if not config["configured"]:
            self.last_error = "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set."
            print("[TG] Token/ChatID not set.")
            return {"status": "error", "reason": self.last_error}
        if self.running:
            return {"status": "already_running"}

        self.running = True
        self._thread = threading.Thread(target=self._main_loop, daemon=True)
        self._thread.start()
        return {"status": "started", "scan_interval_sec": SCAN_INTERVAL}

    def stop(self):
        self.running = False
        self._send("<b>Signal Bot Stopped</b>")
        return {"status": "stopped"}

    def send_test_message(self) -> Dict:
        ok = self._send(
            "<b>Telegram Test</b>\n"
            "Bot credentials are valid and the app can reach Telegram."
        )
        if ok:
            return {"status": "sent"}
        return {"status": "error", "reason": self.last_error or "Telegram send failed"}

    def get_status(self) -> Dict:
        config = _telegram_config()
        return {
            "running": self.running,
            "configured": bool(config["configured"]),
            "chat_id_configured": bool(config["chat_id"]),
            "token_configured": bool(config["token"]),
            "scan_interval_sec": SCAN_INTERVAL,
            "max_trades_per_day": MAX_TRADES_PER_DAY,
            "max_vix": MAX_VIX,
            "last_error": self.last_error,
            "last_send_status": self.last_send_status,
            "last_scan_summary": self.last_scan_summary,
        }

    def scan_once(self, stocks: Optional[List[str]] = None) -> List[Dict]:
        config = _telegram_config()
        if not config["configured"]:
            self.last_error = "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set."
            return [{"error": self.last_error}]
        return self._scan_and_send(stocks)


_bot_instance: Optional[TelegramSignalBot] = None


def get_bot(db=None) -> TelegramSignalBot:
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = TelegramSignalBot(db=db)
    return _bot_instance
