class ChatGPTActuatorError(Exception):
    """Base error for ChatGPT-Actuator."""


class ConfigError(ChatGPTActuatorError):
    """Invalid or unusable configuration."""


class AccessDeniedError(ChatGPTActuatorError):
    """Requested path is outside the configured allowed roots."""


class FileTooLargeError(ChatGPTActuatorError):
    """Requested file exceeds the configured read limit."""


class BinaryFileError(ChatGPTActuatorError):
    """Requested file appears to be binary rather than text."""
