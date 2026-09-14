"""Explicit units, model assumptions, and secrets supplied through environment."""

from datetime import date, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / '.env',
        env_file_encoding='utf-8', extra='ignore', allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    mistral_api_key: SecretStr | None = None
    mistral_model: str = 'mistral-large-latest'
    mistral_timeout: int = Field(default=45, gt=0, le=300)
    input_timezone: str = 'Asia/Kolkata'
    expiry_timezone: str = 'Asia/Kolkata'
    expiry_time: str = '15:30:00'
    timestamp_unit: Literal['s', 'ms', 'us', 'ns'] | None = None
    iv_unit: Literal['decimal', 'percent'] = 'decimal'
    derive_greeks: bool = False
    risk_free_rate: float | None = None
    dividend_yield: float | None = None
    # Any rejected row fails the entire batch by default.
    max_quarantine_fraction: float = Field(default=0, ge=0, le=1)
    max_input_mb: int = Field(default=100, gt=0)
    max_rows: int = Field(default=1_000_000, gt=0)
    s3_bucket: str = 'heremesv0-cleaned-data'
    s3_prefix: str = 'cleaned'
    aws_region: str | None = None
    postgres_host: str = '127.0.0.1'
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_database: str = 'hermes'
    postgres_user: str = 'postgres'
    postgres_password: SecretStr | None = None
    postgres_connect_timeout: int = Field(default=10, ge=1, le=60)
    daily_planner: Literal['deterministic', 'mistral'] = 'deterministic'
    daily_start_date: date | None = None
    daily_lookback_days: int = Field(default=30, ge=1, le=3650)
    daily_ready_time: str = '15:45:00'
    daily_runtime_dir: Path = Path(__file__).resolve().parents[1] / 'runtime'
    daily_output_dir: Path = Path(__file__).resolve().parents[1] / 'output' / 'postgres'
    mistral_keychain_service: str = 'com.openalgo.data-cleaning-agent.mistral'

    @field_validator('input_timezone', 'expiry_timezone')
    @classmethod
    def timezone_exists(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator('expiry_time', 'daily_ready_time')
    @classmethod
    def local_close_time(cls, value: str) -> str:
        parsed = time.fromisoformat(value)
        if parsed.tzinfo is not None:
            raise ValueError('Configured clock time must be local')
        return value

    @model_validator(mode='after')
    def explicit_greek_assumptions(self):
        if self.derive_greeks and (self.risk_free_rate is None or self.dividend_yield is None):
            raise ValueError('DERIVE_GREEKS requires explicit RISK_FREE_RATE and DIVIDEND_YIELD')
        return self
