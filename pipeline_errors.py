"""Safe, provider-independent pipeline errors; no raw input in messages."""

class LLMClientError(Exception):
    """Base for sanitized errors; no credentials or remote bodies in messages."""


class ConfigurationError(LLMClientError):
    pass


class InvalidRequestError(LLMClientError):
    pass


class AuthenticationError(LLMClientError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Yandex AI Studio: ошибка авторизации HTTP {status_code}. Проверьте права и API key.")


class LLMHTTPError(LLMClientError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Yandex AI Studio: HTTP {status_code}. Проверьте модель, параметры и доступность API.")


class LLMTimeoutError(LLMClientError):
    pass


class LLMNetworkError(LLMClientError):
    pass


class InvalidJSONError(LLMClientError):
    pass


class UnexpectedResponseError(LLMClientError):
    pass


class InvalidLLMResponseError(UnexpectedResponseError):
    """The model's response cannot be accepted; no partial result is returned."""


class MissingFieldsError(InvalidLLMResponseError):
    pass


class ExtraFieldsError(InvalidLLMResponseError):
    pass


class SchemaViolationError(InvalidLLMResponseError):
    pass


class PrivacyError(InvalidRequestError):
    pass


class MergeError(ValueError):
    pass


class FinalizationError(ValueError):
    pass


class FinalValidationError(ValueError):
    pass
