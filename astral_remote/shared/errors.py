# Shared error types for CLI, builder, and agent.
# Each error carries a stable machine-readable code so callers can
# branch without parsing message text. Ported from src/shared/errors.js.

class RemoteError(Exception):
    """Base error. Stores a machine-readable code alongside the message."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class ConfigError(RemoteError):
    """Bad or missing configuration value. Message names the field and fix."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "CONFIG_ERROR")


class BuildError(RemoteError):
    """EXE build failure. Message holds actionable cause, no secrets."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "BUILD_ERROR")
