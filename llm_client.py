"""Yandex AI Studio Chat Completions transport, isolated from extraction.

Only environment variables supply credentials/model identifiers. The default
response format is strict JSON Schema, built from FIELD_CONTRACT. JSON object
mode is an explicit option for models supporting only that documented mode;
there is no automatic downgrade/retry after a 400 response.

No merge, final submission generation or missing-value substitution occurs.
"""

import errno
import json
import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


from field_contract import FIELD_CONTRACT


CHAT_COMPLETIONS_URL = "https://ai.api.cloud.yandex.net/v1/chat/completions"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 180.0


from pipeline_errors import (
    LLMClientError, ConfigurationError, InvalidRequestError, AuthenticationError,
    LLMHTTPError, LLMTimeoutError, LLMNetworkError, InvalidJSONError,
    UnexpectedResponseError, InvalidLLMResponseError, MissingFieldsError,
    ExtraFieldsError, SchemaViolationError, PrivacyError,
)
from contract_validation import build_response_schema, validate_response, build_evidence_response_schema, split_llm_response
from privacy_guard import assert_safe_request


@dataclass(frozen=True)
class YandexConfig:
    api_key: str = field(repr=False)
    model_uri: str
    folder_id: str

    @classmethod
    def from_env(cls, environ=None):
        """Read only named variables; never reveal their values in an error."""
        environ = os.environ if environ is None else environ
        if any(not isinstance(environ.get(name, ""), str) for name in ("YANDEX_API_KEY", "YANDEX_MODEL", "YANDEX_FOLDER_ID")):
            raise ConfigurationError("Переменные конфигурации Yandex должны быть строками.")
        key = environ.get("YANDEX_API_KEY", "").strip()
        model = environ.get("YANDEX_MODEL", "").strip()
        folder = environ.get("YANDEX_FOLDER_ID", "").strip()
        missing = [name for name, value in (
            ("YANDEX_API_KEY", key), ("YANDEX_MODEL", model), ("YANDEX_FOLDER_ID", folder)
        ) if not value]
        if missing:
            raise ConfigurationError("Установите переменные окружения: " + ", ".join(missing))
        if re.search(r"[\s\x00-\x1f\x7f]", key):
            raise ConfigurationError("YANDEX_API_KEY содержит недопустимые символы.")
        try:
            key.encode("ascii")
        except UnicodeEncodeError:
            raise ConfigurationError("YANDEX_API_KEY должен содержать ASCII-символы.") from None
        if model.startswith("gpt://"):
            try:
                parsed = urlsplit(model)
            except ValueError:
                raise ConfigurationError("Некорректный URI в YANDEX_MODEL.") from None
            uri_folder = parsed.netloc
            model_name = parsed.path.lstrip("/")
            if parsed.query or parsed.fragment or folder != uri_folder:
                raise ConfigurationError("YANDEX_MODEL и YANDEX_FOLDER_ID должны задавать один каталог без query/fragment.")
        else:
            model_name = model
        if not re.fullmatch(r"[A-Za-z0-9_-]+", folder):
            raise ConfigurationError("Задайте корректный каталог в YANDEX_FOLDER_ID.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", model_name):
            raise ConfigurationError("YANDEX_MODEL должен содержать имя/версию модели или полный gpt:// URI.")
        return cls(key, f"gpt://{folder}/{model_name}", folder)


def _reject_constants(_value):
    raise ValueError("Non-JSON constant")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _parse_json(raw, *, envelope=False):
    try:
        return json.loads(raw, parse_constant=_reject_constants, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        location = "HTTP-ответ API" if envelope else "message.content модели"
        raise InvalidJSONError(location + " содержит некорректный JSON.") from None


def _model_result(raw, targets, schema, api_key, field_contract=FIELD_CONTRACT, *, evidence_mode=False):
    # API error bodies and model content are never put into an error message.
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise UnexpectedResponseError("HTTP-ответ имеет неподдерживаемый тип или слишком большой размер.")
    envelope = _parse_json(raw, envelope=True)
    if not isinstance(envelope, dict):
        raise UnexpectedResponseError("Chat Completions вернул неожиданный объект ответа.")
    choices = envelope.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise UnexpectedResponseError("Chat Completions должен вернуть один элемент choices.")
    choice = choices[0]
    message = choice.get("message")
    if choice.get("finish_reason") != "stop":
        raise UnexpectedResponseError("Генерация не завершена нормально: возможен лимит токенов, отказ или вызов инструмента.")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise UnexpectedResponseError("В ответе отсутствует строковый message.content.")
    if message.get("refusal") or message.get("tool_calls"):
        raise UnexpectedResponseError("Модель вернула отказ или вызов инструмента вместо данных.")
    result = _parse_json(message["content"])
    if api_key in json.dumps(result, ensure_ascii=False):
        raise UnexpectedResponseError("Ответ содержит защищённое значение и не может быть передан дальше.")
    assert_safe_request({"llm_required": True, "system_prompt": "", "user_prompt": json.dumps(result, ensure_ascii=False)})
    if evidence_mode:
        split_llm_response(result, targets, evidence_mode=True, field_contract=field_contract)
        return result
    return validate_response(result, targets, schema, field_contract)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Authorization to a redirected destination.
        return None


def _open_http(request, *, timeout):
    return build_opener(_NoRedirect()).open(request, timeout=timeout)


def _resolve_timeout(timeout):
    """An explicit client timeout overrides the environment; otherwise use 180 s."""
    name = "timeout"
    if timeout is None:
        name = "YANDEX_TIMEOUT_SECONDS"
        try:
            timeout = float(os.environ.get(name, str(DEFAULT_TIMEOUT_SECONDS)))
        except (TypeError, ValueError, OverflowError):
            raise ConfigurationError(name + " должен быть положительным конечным числом секунд.") from None
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
        raise ConfigurationError(name + " должен быть положительным конечным числом секунд.")
    return timeout


class YandexLLMClient:
    def __init__(self, config=None, *, field_contract=FIELD_CONTRACT,
                 response_mode="json_schema", timeout=None, max_attempts=3,
                 retry_delay=0.5, temperature=0.0, max_completion_tokens=4096,
                 transport=None, sleep=None):
        if response_mode not in {"json_schema", "json_object"}:
            raise ConfigurationError("response_mode должен быть json_schema или json_object.")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 3:
            raise ConfigurationError("max_attempts должен быть целым числом от 1 до 3.")
        timeout = _resolve_timeout(timeout)
        if type(retry_delay) not in {int, float} or not math.isfinite(retry_delay) or not 0 <= retry_delay <= 5:
            raise ConfigurationError("retry_delay должен быть числом от 0 до 5 секунд.")
        if temperature is not None and (type(temperature) not in {int, float} or not math.isfinite(temperature) or not 0 <= temperature <= 0.3):
            raise ConfigurationError("Для extraction temperature должна быть от 0 до 0.3 либо None.")
        if type(max_completion_tokens) is not int or max_completion_tokens <= 0:
            raise ConfigurationError("max_completion_tokens должен быть положительным целым числом.")
        if config is not None:
            if not isinstance(config, YandexConfig):
                raise ConfigurationError("config должен быть YandexConfig.")
            config = YandexConfig.from_env({"YANDEX_API_KEY": config.api_key,
                                            "YANDEX_MODEL": config.model_uri,
                                            "YANDEX_FOLDER_ID": config.folder_id})
        self._config = config  # lazy env lookup lets a skipped task need no key
        self.field_contract = field_contract
        self.response_mode = response_mode
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay
        self.temperature = temperature
        self.max_completion_tokens = max_completion_tokens
        self._transport = _open_http if transport is None else transport
        self._sleep = time.sleep if sleep is None else sleep

    def _post(self, request):
        for attempt in range(self.max_attempts):
            failure, transient = None, False
            try:
                with self._transport(request, timeout=self.timeout) as response:
                    return response.read(MAX_RESPONSE_BYTES + 1)
            except HTTPError as error:
                code = error.code
                try:
                    error.close()
                except OSError:
                    pass  # Preserve the safe HTTP error even if stream cleanup fails.
                failure = AuthenticationError(code) if code in {401, 403} else LLMHTTPError(code)
                transient = code == 429 or 500 <= code <= 599
            except TimeoutError:
                failure, transient = LLMTimeoutError("Истекло время ожидания ответа Yandex AI Studio."), True
            except URLError as error:
                if isinstance(error.reason, TimeoutError):
                    failure, transient = LLMTimeoutError("Истекло время ожидания ответа Yandex AI Studio."), True
                else:
                    failure = LLMNetworkError("Сетевая ошибка соединения с Yandex AI Studio.")
                    transient = isinstance(error.reason, ConnectionError)
            except ConnectionError:
                failure, transient = LLMNetworkError("Соединение с Yandex AI Studio прервано."), True
            except OSError as error:
                failure = LLMNetworkError("Ошибка сетевого транспорта Yandex AI Studio.")
                transient = error.errno in {errno.ECONNRESET, errno.ETIMEDOUT}
            except Exception:
                failure = LLMNetworkError("Неожиданная ошибка HTTP-транспорта Yandex AI Studio.")
            if transient and attempt + 1 < self.max_attempts:
                self._sleep(self.retry_delay * (2 ** attempt))
                continue
            # Raise outside except so raw transport exceptions are not chained.
            raise failure from None

    def call(self, llm_request):
        if not isinstance(llm_request, Mapping):
            raise InvalidRequestError("Ожидается объект из build_llm_request.")
        if llm_request.get("llm_required") is False:
            return {}
        if llm_request.get("llm_required") is not True:
            raise InvalidRequestError("В запросе нужен булевый llm_required.")
        targets = llm_request.get("target_fields")
        evidence_mode = llm_request.get("include_evidence", False)
        if type(evidence_mode) is not bool:
            raise InvalidRequestError("include_evidence must be boolean.")
        schema = (build_evidence_response_schema(targets, self.field_contract) if evidence_mode
                  else build_response_schema(targets, self.field_contract))
        if not targets:
            raise InvalidRequestError("llm_required=True требует непустые target_fields.")
        confirmed = llm_request.get("confirmed_values", {})
        if not isinstance(confirmed, Mapping) or set(confirmed).intersection(targets):
            raise InvalidRequestError("Цели не должны пересекаться с confirmed_values.")
        if any(self.field_contract[name].get("extractor") == "parser" for name in targets):
            raise InvalidRequestError("Поля маршрута parser нельзя передавать модели для заполнения.")
        messages = []
        for role, key in (("system", "system_prompt"), ("user", "user_prompt")):
            content = llm_request.get(key)
            if not isinstance(content, str) or not content.strip():
                raise InvalidRequestError("Нужны непустые system_prompt и user_prompt.")
            messages.append({"role": role, "content": content})
        assert_safe_request(llm_request)
        config = self._config if self._config is not None else YandexConfig.from_env()
        body = {
            "model": config.model_uri,
            "messages": messages,
            "stream": False,
            "max_completion_tokens": self.max_completion_tokens,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.response_mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "epicrisis_extraction", "schema": schema, "strict": True},
            }
        else:
            # Explicitly selected documented JSON mode, still locally validated.
            body["response_format"] = {"type": "json_object"}
        try:
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise InvalidRequestError("HTTP body не может быть сериализован в JSON.") from None
        if config.api_key.encode("ascii") in encoded:
            raise InvalidRequestError("Секрет не должен содержаться в JSON-теле запроса.")
        request = Request(CHAT_COMPLETIONS_URL, data=encoded, method="POST", headers={
            "Authorization": "Api-Key " + config.api_key,
            "Content-Type": "application/json",
            "OpenAI-Project": config.folder_id,
        })
        raw = self._post(request)
        return _model_result(raw, targets, schema, config.api_key, self.field_contract, evidence_mode=evidence_mode)


def call_llm(llm_request, **client_options):
    """Return target values as dict; a skipped task returns {} without env/HTTP."""
    if isinstance(llm_request, Mapping) and llm_request.get("llm_required") is False:
        return {}
    return YandexLLMClient(**client_options).call(llm_request)
