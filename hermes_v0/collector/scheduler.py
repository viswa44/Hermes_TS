"""Clock-aligned 5-second scheduler for Hermes V0.

Ensures snapshots are collected at exact 5-second boundaries (09:15:00, 09:15:05, etc.)
without drift from processing time.
"""

import asyncio
import time
import logging
from datetime import datetime, timedelta
from typing import AsyncIterator, Callable, Optional
from zoneinfo import ZoneInfo

from hermes_v0.config import CFG


IST = ZoneInfo("Asia/Kolkata")


class ClockAlignedScheduler:
    """Produces timestamps aligned to 5-second boundaries during market hours.
    
    Unlike sleep(5) loops, this calculates the next boundary and sleeps
    exactly the required duration, preventing drift.
    """
    
    def __init__(
        self,
        interval_seconds: int = CFG.collector.interval_seconds,
        market_open: str = CFG.collector.market_open,
        market_close: str = CFG.collector.market_close,
        timezone: str = CFG.collector.timezone,
        max_tick_lateness_seconds: float = 0.5,
    ):
        self.interval_seconds = interval_seconds
        self.market_open = self._parse_time(market_open)
        self.market_close = self._parse_time(market_close)
        self.tz = ZoneInfo(timezone)
        self._running = False
        self.max_tick_lateness_seconds = max_tick_lateness_seconds
    
    def _parse_time(self, time_str: str) -> tuple[int, int]:
        """Parse HH:MM string to (hour, minute)."""
        parts = time_str.split(":")
        return int(parts[0]), int(parts[1])
    
    def _now_ist(self) -> datetime:
        """Current time in IST."""
        return datetime.now(self.tz)
    
    def _is_market_hours(self, dt: datetime) -> bool:
        """Check if datetime is within market hours."""
        if dt.weekday() >= 5:  # Weekend
            return False
        
        current_minute = dt.hour * 60 + dt.minute
        open_min = self.market_open[0] * 60 + self.market_open[1]
        close_min = self.market_close[0] * 60 + self.market_close[1]
        
        return open_min <= current_minute < close_min
    
    def _next_boundary(self, dt: datetime) -> datetime:
        """Calculate the next 5-second boundary after dt."""
        # Round up to next 5-second boundary
        seconds = dt.second
        next_5s = ((seconds // self.interval_seconds) + 1) * self.interval_seconds
        
        if next_5s >= 60:
            # Next minute
            next_dt = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        else:
            next_dt = dt.replace(second=next_5s, microsecond=0)
        
        return next_dt
    
    def _trading_date(self, dt: datetime) -> str:
        """Get trading date (YYYY-MM-DD) for a timestamp."""
        # If before market open, previous trading day
        open_min = self.market_open[0] * 60 + self.market_open[1]
        current_min = dt.hour * 60 + dt.minute
        
        if current_min < open_min:
            # Before market open, use previous weekday
            prev = dt - timedelta(days=1)
            while prev.weekday() >= 5:
                prev -= timedelta(days=1)
            return prev.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    
    async def tick_generator(self) -> AsyncIterator[tuple[datetime, str]]:
        """Async generator yielding (timestamp_ist, trading_date) at each 5s boundary."""
        self._running = True
        
        while self._running:
            now = self._now_ist()
            
            # Check market hours
            if not self._is_market_hours(now):
                # After-close/wake-from-sleep must exit instead of waiting overnight.
                if now.weekday() >= 5 or (now.hour, now.minute) >= self.market_close:
                    self._running = False
                    break
                # Sleep until next market open check (1 minute)
                await asyncio.sleep(60)
                continue
            
            # Calculate next boundary
            next_boundary = self._next_boundary(now)
            
            # If next boundary is outside market hours, stop
            if not self._is_market_hours(next_boundary):
                self._running = False
                break
            
            # Sleep until boundary
            sleep_seconds = (next_boundary - now).total_seconds()
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
            
            # Verify we're still at the boundary (account for async scheduling)
            actual = self._now_ist()
            if not self._is_market_hours(actual):
                self._running = False
                break
            if (actual - next_boundary).total_seconds() > self.max_tick_lateness_seconds:
                logging.getLogger(__name__).warning("MISSED_INTERVAL: late scheduler wake; no observation fabricated")
                continue
            drift_ms = abs((actual - next_boundary).total_seconds()) * 1000
            
            trading_date = self._trading_date(next_boundary)
            
            yield next_boundary, trading_date
    
    def stop(self):
        """Stop the scheduler."""
        self._running = False
    
    def is_market_open_now(self) -> bool:
        """Check if market is currently open."""
        return self._is_market_hours(self._now_ist())


class DriftMonitor:
    """Monitors timing drift for a configured clock-aligned interval."""
    
    def __init__(self, max_drift_ms: float = 100.0, interval_seconds: int = 5):
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.max_drift_ms = max_drift_ms
        self.interval_seconds = interval_seconds
        self.drifts: list[float] = []
        self.missed_intervals: int = 0
        self.last_expected: Optional[datetime] = None
    
    def record(self, expected: datetime, actual: datetime):
        """Record drift for a collection."""
        drift_ms = (actual - expected).total_seconds() * 1000
        self.drifts.append(drift_ms)
        
        # Check for missed intervals
        if self.last_expected is not None:
            expected_intervals = int(
                (expected - self.last_expected).total_seconds() / self.interval_seconds
            )
            if expected_intervals > 1:
                self.missed_intervals += expected_intervals - 1
        
        self.last_expected = expected
        
        # Keep only last 1000 drifts
        if len(self.drifts) > 1000:
            self.drifts = self.drifts[-1000:]
    
    def get_stats(self) -> dict:
        """Get drift statistics."""
        if not self.drifts:
            return {"avg_drift_ms": 0, "max_drift_ms": 0, "missed_intervals": 0}
        
        return {
            "avg_drift_ms": sum(self.drifts) / len(self.drifts),
            "max_drift_ms": max(self.drifts),
            "min_drift_ms": min(self.drifts),
            "missed_intervals": self.missed_intervals,
            "samples": len(self.drifts),
        }
    
    def is_healthy(self) -> bool:
        """Check if drift is within acceptable bounds."""
        if not self.drifts:
            return True
        return max(abs(d) for d in self.drifts) <= self.max_drift_ms
