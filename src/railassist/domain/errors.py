class RailAssistError(Exception):
    """An actionable application error."""


class ConfigError(RailAssistError):
    pass


class CapabilityUnavailable(RailAssistError):
    pass


class InstanceBusy(RailAssistError):
    pass


class InvalidTransition(RailAssistError):
    pass


class RateLimitedError(RailAssistError):
    """请求频繁（429 类）；retry_after_seconds 为 None 时使用默认冷却。"""

    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class TransientQueryError(RailAssistError):
    """查询超时或临时 5xx，可按退避策略重试。"""


class VerificationRequired(RailAssistError):
    """官方要求人工核验；自动操作必须暂停。"""
