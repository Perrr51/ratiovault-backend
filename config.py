"""
Configuration management for the FastAPI backend.
Loads settings from environment variables with sensible defaults.
"""

import os
from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # CORS Configuration
    cors_origins: str = "http://localhost:5173,http://localhost:5174,http://localhost:3000"

    # SEC API Configuration
    sec_user_agent: str = "ThinkInvest contact@thinkinvest.com"

    # Cache Configuration
    chart_cache_ttl: int = 300  # 5 minutes in seconds
    chart_cache_max_size: int = 100  # Maximum number of cached items

    # Logging Configuration
    log_level: str = "INFO"

    # Server Configuration
    port: int = 8000
    host: str = "0.0.0.0"

    class Config:
        env_file = ".env"
        case_sensitive = False

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse CORS origins from comma-separated string"""
        return [origin.strip() for origin in self.cors_origins.split(",")]


# Global settings instance
settings = Settings()


def validate_settings():
    """Validate critical settings on application startup"""
    if not settings.sec_user_agent or "contact@example.com" in settings.sec_user_agent:
        raise ValueError(
            "SEC_USER_AGENT must be configured with a valid email address. "
            "Update your .env file with: SEC_USER_AGENT='YourApp contact@youremail.com'"
        )

    if settings.chart_cache_ttl < 0:
        raise ValueError("CHART_CACHE_TTL must be a positive number")

    if settings.chart_cache_max_size < 1:
        raise ValueError("CHART_CACHE_MAX_SIZE must be at least 1")

    print("✅ Configuration validated successfully")
    print(f"📊 CORS Origins: {settings.cors_origins_list}")
    print(f"💾 Cache TTL: {settings.chart_cache_ttl}s")
    print(f"📦 Cache Max Size: {settings.chart_cache_max_size}")
    print(f"🔒 SEC User Agent: {settings.sec_user_agent}")
