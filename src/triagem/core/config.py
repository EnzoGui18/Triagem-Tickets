from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import NonNegativeInt, PositiveFloat, PositiveInt, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Triagem de Tickets"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    metrics_port: PositiveInt = 8000
    database_url: str = "postgresql+asyncpg://triagem:triagem@localhost:5432/triagem"
    database_pool_size: PositiveInt = 5
    database_max_overflow: NonNegativeInt = 10
    database_pool_timeout_seconds: PositiveFloat = 5.0
    redis_url: str = "redis://localhost:6379/0"
    llm_base_url: str = "http://llm-sim:8080"
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "triagem-sim-1"
    llm_timeout_seconds: float = 10.0
    llm_rate_limit_rps: PositiveInt = 5
    llm_max_concurrency: PositiveInt = 5
    llm_circuit_breaker_failure_threshold: PositiveInt = 2
    llm_circuit_breaker_recovery_timeout_seconds: PositiveFloat = 10.0
    classification_worker_lease_seconds: float = 60.0
    classification_worker_retry_delay_seconds: float = 5.0
    classification_worker_max_retry_delay_seconds: float = 300.0
    classification_worker_poll_interval_seconds: float = 0.5
    plantao_base_url: str = "http://plantao:8080"
    plantao_timeout_seconds: float = 10.0
    alert_worker_lease_seconds: float = 30.0
    alert_worker_retry_delay_seconds: float = 5.0
    alert_worker_max_retry_delay_seconds: float = 300.0
    alert_worker_poll_interval_seconds: float = 0.5


@lru_cache
def get_settings() -> Settings:
    return Settings()
