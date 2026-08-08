"""Long-running shadow monitor orchestration, independent of FastAPI."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from pathlib import Path
import time as monotonic_time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.breakouts.live.candidates import (
    build_daily_candidate_snapshot,
    candidates_from_snapshot,
)
from src.breakouts.live.detector import (
    ALGORITHM_VERSION,
    PARAMETER_VERSION,
    BreakoutDetector,
)
from src.breakouts.live.feeds import FmpRestFeed, IntradayFeed
from src.breakouts.live.models import (
    DailyCandidate,
    MonitorSymbolState,
    QuoteSnapshot,
)
from src.breakouts.live.rolling import RollingIntradayBars
from src.breakouts.live.selector import select_active_pool
from src.breakouts.live.session import expected_source_session
from src.breakouts.live.settings import IntradayMonitorSettings
from src.breakouts.live.state import IntradayMonitorState
from src.utils.io import atomic_save_json


CandidateBuilder = Callable[..., dict[str, Any]]
SourceSessionResolver = Callable[[str], str]


class IntradayMomentumMonitor:
    def __init__(
        self,
        settings: IntradayMonitorSettings,
        *,
        feed: IntradayFeed | None = None,
        state: IntradayMonitorState | None = None,
        candidate_builder: CandidateBuilder = build_daily_candidate_snapshot,
        source_session_resolver: SourceSessionResolver = expected_source_session,
    ) -> None:
        self.settings = settings.validate()
        self.feed = feed or FmpRestFeed(
            quote_chunk_size=settings.quote_chunk_size,
            max_concurrent_requests=settings.max_concurrent_requests,
        )
        self.state = state or IntradayMonitorState(settings.state_path)
        self.candidate_builder = candidate_builder
        self.source_session_resolver = source_session_resolver
        self.detector = BreakoutDetector(settings)
        self.timezone = ZoneInfo(settings.timezone)
        self.candidate_snapshot: dict[str, Any] | None = None
        self.candidates: list[DailyCandidate] = []
        self.active_tickers: list[str] = []
        self.rolling: dict[str, RollingIntradayBars] = {}
        self.quotes: dict[str, QuoteSnapshot] = {}
        self.last_broad_at: datetime | None = None
        self.last_market_check_at: datetime | None = None
        self.market_status: dict[str, Any] = {"isMarketOpen": False}
        self.last_exact_confirm: dict[str, datetime] = {}
        self.started_at = datetime.now(self.timezone)
        self.cycle_count = 0

    async def _ensure_candidates(self, session_date: str) -> None:
        source_session = self.source_session_resolver(session_date)
        if (
            self.candidate_snapshot is not None
            and self.candidate_snapshot.get("session_date") == session_date
            and self.candidate_snapshot.get("source_data_date") == source_session
        ):
            return
        snapshot = self.state.load_candidate_snapshot(
            session_date,
            ALGORITHM_VERSION,
            PARAMETER_VERSION,
        )
        if (
            snapshot is not None
            and snapshot.get("source_data_date") != source_session
        ):
            snapshot = None
        if snapshot is None:
            snapshot = await asyncio.to_thread(
                self.candidate_builder,
                self.settings,
                session_date=session_date,
                source_session=source_session,
            )
            if snapshot.get("source_data_date") != source_session:
                raise RuntimeError(
                    "candidate builder returned the wrong source session"
                )
            self.state.save_candidate_snapshot(snapshot)
        self.candidate_snapshot = snapshot
        self.candidates = candidates_from_snapshot(snapshot)
        self.active_tickers = []
        self.rolling = {}
        self.quotes = {}
        self.last_broad_at = None

    def _broad_due(self, now: datetime) -> bool:
        return (
            self.last_broad_at is None
            or now - self.last_broad_at
            >= timedelta(minutes=self.settings.broad_refresh_minutes)
        )

    async def _refresh_market_status(self, now: datetime, *, force: bool) -> None:
        due = (
            force
            or self.last_market_check_at is None
            or now - self.last_market_check_at >= timedelta(minutes=1)
        )
        if not due:
            return
        try:
            self.market_status = await self.feed.market_status("NASDAQ")
        except Exception as exc:  # noqa: BLE001
            self.market_status = {
                "isMarketOpen": False,
                "error": type(exc).__name__,
            }
        self.last_market_check_at = now

    async def _merge_exact(
        self,
        tickers: list[str],
        *,
        session_date: str,
        preload: bool = False,
    ) -> tuple[list[str], list[str]]:
        if not tickers:
            return [], []
        frames = await self.feed.intraday_many(
            tickers,
            session_date=session_date,
            preload=preload,
        )
        loaded: list[str] = []
        failed: list[str] = []
        for ticker in tickers:
            frame = frames.get(ticker)
            if frame is None or frame.empty:
                failed.append(ticker)
                continue
            rolling = self.rolling.setdefault(
                ticker,
                RollingIntradayBars(ticker, timezone=self.settings.timezone),
            )
            rolling.merge(frame)
            loaded.append(ticker)
        return loaded, failed

    def _heartbeat_payload(
        self,
        *,
        now: datetime,
        session_date: str,
        phase: str,
        cycle_seconds: float | None = None,
        errors: list[str] | None = None,
    ) -> dict[str, Any]:
        source_data_date = (
            self.candidate_snapshot.get("source_data_date")
            if self.candidate_snapshot
            else None
        )
        return {
            "mode": "shadow",
            "phase": phase,
            "algorithm_version": ALGORITHM_VERSION,
            "parameter_version": PARAMETER_VERSION,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "session_date": session_date,
            "source_data_date": source_data_date,
            "market_open": bool(self.market_status.get("isMarketOpen")),
            "feed": self.feed.source_name,
            "candidate_count": len(self.candidates),
            "active_count": len(self.active_tickers),
            "active_tickers": list(self.active_tickers),
            "stored_bar_count": sum(
                rolling.stored_bars for rolling in self.rolling.values()
            ),
            "cycle_count": self.cycle_count,
            "cycle_seconds": cycle_seconds,
            "feed_counters": self.feed.counters(),
            "errors": list(errors or []),
            "observed_at": now.isoformat(timespec="seconds"),
        }

    def _save_cycle_snapshot(
        self,
        payload: dict[str, Any],
        *,
        session_date: str,
    ) -> Path:
        path = self.settings.snapshots_dir / f"{session_date}.json"
        atomic_save_json(payload, path)
        return path

    async def cycle(
        self,
        *,
        now: datetime | None = None,
        allow_closed: bool = False,
        force_broad: bool = False,
    ) -> dict[str, Any]:
        cycle_started = monotonic_time.perf_counter()
        aware_now = (now or datetime.now(self.timezone)).astimezone(self.timezone)
        session_date = aware_now.strftime("%Y-%m-%d")
        errors: list[str] = []
        broad_due = force_broad or self._broad_due(aware_now)
        await self._refresh_market_status(aware_now, force=broad_due)
        market_open = bool(self.market_status.get("isMarketOpen"))
        if not market_open and not allow_closed:
            payload = self._heartbeat_payload(
                now=aware_now,
                session_date=session_date,
                phase="market_closed",
            )
            self.state.heartbeat(payload)
            return payload
        await self._ensure_candidates(session_date)

        if broad_due:
            cycle_quotes = await self.feed.quotes(
                candidate.ticker for candidate in self.candidates
            )
            self.quotes.update(cycle_quotes)
            selections = select_active_pool(
                self.candidates,
                self.quotes,
                max_symbols=self.settings.active_max_symbols,
                previous_tickers=self.active_tickers,
            )
            selected_tickers = [
                selection.candidate.ticker for selection in selections
            ]
            previous = set(self.active_tickers)
            self.active_tickers = selected_tickers
            new_tickers = [
                ticker for ticker in selected_tickers if ticker not in previous
            ]
            preload_loaded, preload_failed = await self._merge_exact(
                new_tickers,
                session_date=session_date,
                preload=True,
            )
            errors.extend(f"preload:{ticker}" for ticker in preload_failed)
            retained_tickers = [
                ticker for ticker in selected_tickers if ticker in previous
            ]
            refresh_loaded, refresh_failed = await self._merge_exact(
                retained_tickers,
                session_date=session_date,
                preload=False,
            )
            errors.extend(f"refresh:{ticker}" for ticker in refresh_failed)
            fresh_exact = set(preload_loaded) | set(refresh_loaded)
            self.last_broad_at = aware_now
        else:
            cycle_quotes = await self.feed.quotes(self.active_tickers)
            self.quotes.update(cycle_quotes)
            new_tickers = []
            preload_loaded = []
            fresh_exact = set()

        candidate_map = {
            candidate.ticker: candidate for candidate in self.candidates
        }
        confirm: list[str] = []
        metrics_by_ticker: dict[str, dict[str, Any]] = {}
        symbol_updates: list[
            tuple[str, MonitorSymbolState, dict[str, Any]]
        ] = []
        for ticker in self.active_tickers:
            candidate = candidate_map.get(ticker)
            quote = self.quotes.get(ticker)
            rolling = self.rolling.get(ticker)
            if candidate is None or quote is None or rolling is None:
                continue
            metrics = rolling.metrics(
                now=aware_now,
                session_date=session_date,
                interval=self.settings.detector_interval_minutes,
            )
            metrics_by_ticker[ticker] = metrics
            armed = self.detector.should_confirm(
                candidate,
                quote,
                metrics,
                now=aware_now,
                session_date=session_date,
            )
            symbol_updates.append((
                ticker,
                (
                    MonitorSymbolState.ARMED
                    if armed
                    else MonitorSymbolState.WATCHING
                ),
                {
                    "quote_timestamp": quote.timestamp.isoformat(timespec="seconds"),
                    "last_bar": metrics.get("last_timestamp"),
                },
            ))
            if not armed:
                continue
            last_confirm = self.last_exact_confirm.get(ticker)
            if (
                ticker in fresh_exact
                or last_confirm is None
                or (
                    aware_now - last_confirm
                ).total_seconds() >= self.settings.exact_confirm_cooldown_seconds
            ):
                if ticker not in fresh_exact:
                    confirm.append(ticker)
        self.state.set_symbol_states(
            session_date=session_date,
            algorithm_version=ALGORITHM_VERSION,
            rows=symbol_updates,
        )

        loaded, exact_failed = await self._merge_exact(
            confirm,
            session_date=session_date,
            preload=False,
        )
        errors.extend(f"confirm:{ticker}" for ticker in exact_failed)
        for ticker in confirm:
            self.last_exact_confirm[ticker] = aware_now

        new_signals: list[dict[str, Any]] = []
        evaluable = set(loaded) | {
            ticker
            for ticker in fresh_exact
            if ticker in metrics_by_ticker
            and self.detector.should_confirm(
                candidate_map[ticker],
                self.quotes[ticker],
                metrics_by_ticker[ticker],
                now=aware_now,
                session_date=session_date,
            )
        }
        for ticker in sorted(evaluable):
            candidate = candidate_map.get(ticker)
            quote = self.quotes.get(ticker)
            rolling = self.rolling.get(ticker)
            if candidate is None or quote is None or rolling is None:
                continue
            metrics = rolling.metrics(
                now=aware_now,
                session_date=session_date,
                interval=self.settings.detector_interval_minutes,
            )
            signal = self.detector.evaluate(
                candidate,
                quote,
                metrics,
                now=aware_now,
                session_date=session_date,
                market_open=market_open,
            )
            if signal is None:
                continue
            self.state.set_symbol_state(
                session_date=session_date,
                ticker=ticker,
                algorithm_version=ALGORITHM_VERSION,
                state=MonitorSymbolState.TRIGGERED,
                payload=signal.to_dict(),
            )
            inserted = self.state.record_signal(signal, delivery_state="SHADOW")
            self.state.set_symbol_state(
                session_date=session_date,
                ticker=ticker,
                algorithm_version=ALGORITHM_VERSION,
                state=MonitorSymbolState.COOLDOWN,
                payload={"signal_recorded": inserted},
            )
            if inserted:
                new_signals.append(signal.to_dict())

        self.cycle_count += 1
        elapsed = monotonic_time.perf_counter() - cycle_started
        payload = self._heartbeat_payload(
            now=aware_now,
            session_date=session_date,
            phase="completed",
            cycle_seconds=round(elapsed, 3),
            errors=errors,
        )
        payload["new_signals"] = new_signals
        payload["new_signal_count"] = len(new_signals)
        payload["broad_refreshed"] = broad_due
        payload["exact_confirmed"] = len(loaded) + len(fresh_exact)
        self.state.heartbeat(payload)
        self._save_cycle_snapshot(payload, session_date=session_date)
        return payload

    async def run_forever(self) -> None:
        while True:
            now = datetime.now(self.timezone)
            local_time = now.time()
            if local_time >= time(16, 5):
                payload = self._heartbeat_payload(
                    now=now,
                    session_date=now.strftime("%Y-%m-%d"),
                    phase="completed_session",
                )
                self.state.heartbeat(payload)
                return
            if local_time < time(9, 30):
                payload = self._heartbeat_payload(
                    now=now,
                    session_date=now.strftime("%Y-%m-%d"),
                    phase="waiting_for_open",
                )
                self.state.heartbeat(payload)
                await asyncio.sleep(self.settings.heartbeat_seconds)
                continue
            await self.cycle(now=now)
            after = datetime.now(self.timezone)
            next_minute = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
            wake_at = next_minute + timedelta(
                seconds=self.settings.poll_offset_seconds
            )
            while datetime.now(self.timezone) < wake_at:
                remaining = (wake_at - datetime.now(self.timezone)).total_seconds()
                await asyncio.sleep(min(self.settings.heartbeat_seconds, max(0.1, remaining)))
                heartbeat_now = datetime.now(self.timezone)
                if heartbeat_now < wake_at:
                    payload = self._heartbeat_payload(
                        now=heartbeat_now,
                        session_date=heartbeat_now.strftime("%Y-%m-%d"),
                        phase="waiting_next_bar",
                    )
                    self.state.heartbeat(payload)
