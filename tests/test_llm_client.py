from field_contract import NOT_SPECIFIED
"""No live transport: every tested HTTP call uses an in-memory stub."""
import io
import json
import os
import traceback
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from field_contract import FIELD_CONTRACT
from llm_client import (
    YandexConfig, YandexLLMClient, call_llm, build_response_schema,
    CHAT_COMPLETIONS_URL, MAX_RESPONSE_BYTES,
    ConfigurationError, InvalidRequestError, AuthenticationError,
    LLMHTTPError, LLMTimeoutError, LLMNetworkError, InvalidJSONError,
    MissingFieldsError, ExtraFieldsError, SchemaViolationError, UnexpectedResponseError,
    PrivacyError,
)

TEST_KEY = "unit-test-placeholder-not-a-real-key"
ENV = {"YANDEX_API_KEY": TEST_KEY, "YANDEX_MODEL": "test-model/latest", "YANDEX_FOLDER_ID": "test-folder"}


def request_for(*targets):
    return {"llm_required": True, "target_fields": list(targets), "confirmed_values": {},
            "system_prompt": "Extract only the requested fields. Return JSON.",
            "user_prompt": "Обезличенный синтетический эпикриз. АД 120/80."}


def response_bytes(values=None, *, content=None, finish="stop"):
    if content is None:
        content = json.dumps(values, ensure_ascii=False, allow_nan=False)
    return json.dumps({"choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": content}}]}, ensure_ascii=False).encode("utf-8")


def http_error(code):
    return HTTPError(CHAT_COMPLETIONS_URL, code, TEST_KEY, {"Authorization": TEST_KEY}, io.BytesIO(TEST_KEY.encode()))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.config = YandexConfig.from_env(ENV)
        self.transport = Mock(return_value=io.BytesIO(response_bytes({"ca_fact": "Y"})))
        self.sleep = Mock()
        self.client = YandexLLMClient(config=self.config, transport=self.transport, sleep=self.sleep, timeout=180)

    def call(self, values, *targets):
        self.transport.return_value = io.BytesIO(response_bytes(values))
        return self.client.call(request_for(*targets))

    def test_success_structured_response(self):
        self.assertEqual(self.call({"ca_fact": "Y"}, "ca_fact"), {"ca_fact": "Y"})
        body = json.loads(self.transport.call_args.args[0].data)
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])

    def test_headers_endpoint_and_model(self):
        self.client.call(request_for("ca_fact"))
        req = self.transport.call_args.args[0]
        headers = {key.lower(): value for key, value in req.header_items()}
        self.assertEqual(req.full_url, CHAT_COMPLETIONS_URL)
        self.assertEqual(req.method, "POST")
        self.assertEqual(headers["authorization"], "Api-Key " + TEST_KEY)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["openai-project"], "test-folder")
        self.assertEqual(json.loads(req.data)["model"], "gpt://test-folder/test-model/latest")
        self.assertNotIn(TEST_KEY.encode(), req.data)

    def test_messages_and_low_temperature(self):
        request = request_for("ca_fact")
        self.client.call(request)
        body = json.loads(self.transport.call_args.args[0].data)
        self.assertEqual(body["messages"], [{"role": "system", "content": request["system_prompt"]},
                                             {"role": "user", "content": request["user_prompt"]}])
        self.assertEqual(body["temperature"], 0)
        self.assertFalse(body["stream"])

    def test_missing_key(self):
        with self.assertRaisesRegex(ConfigurationError, "YANDEX_API_KEY"):
            YandexConfig.from_env({key: value for key, value in ENV.items() if key != "YANDEX_API_KEY"})

    def test_missing_model(self):
        with self.assertRaisesRegex(ConfigurationError, "YANDEX_MODEL"):
            YandexConfig.from_env({key: value for key, value in ENV.items() if key != "YANDEX_MODEL"})

    def test_missing_folder(self):
        with self.assertRaisesRegex(ConfigurationError, "YANDEX_FOLDER_ID"):
            YandexConfig.from_env({key: value for key, value in ENV.items() if key != "YANDEX_FOLDER_ID"})

    def test_uri_folder_mismatch(self):
        with self.assertRaises(ConfigurationError):
            YandexConfig.from_env({**ENV, "YANDEX_MODEL": "gpt://another-folder/model"})

    def test_model_uri_preserved(self):
        config = YandexConfig.from_env({**ENV, "YANDEX_MODEL": "gpt://test-folder/test-model/latest"})
        self.assertEqual(config.model_uri, self.config.model_uri)

    def test_key_hidden_in_config_repr(self):
        self.assertNotIn(TEST_KEY, repr(self.config))

    def test_bad_key_safe_error(self):
        with self.assertRaises(ConfigurationError) as caught:
            YandexConfig.from_env({**ENV, "YANDEX_API_KEY": TEST_KEY + "\nInjected: unsafe"})
        self.assertNotIn(TEST_KEY, str(caught.exception))

    def test_default_timeout_180(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(YandexLLMClient(transport=self.transport).timeout, 180)

    def test_env_timeout_override(self):
        with patch.dict(os.environ, {"YANDEX_TIMEOUT_SECONDS": "241.5"}, clear=True):
            self.assertEqual(YandexLLMClient(transport=self.transport).timeout, 241.5)

    def test_invalid_timeouts(self):
        for value in ["bad", "", "0", "-1", "nan", "inf", "-inf"]:
            with self.subTest(value=value), patch.dict(os.environ, {"YANDEX_TIMEOUT_SECONDS": value}, clear=True):
                with self.assertRaisesRegex(ConfigurationError, "YANDEX_TIMEOUT_SECONDS"):
                    YandexLLMClient(transport=self.transport)

    def test_timeout_passed_to_transport(self):
        with patch.dict(os.environ, {**ENV, "YANDEX_TIMEOUT_SECONDS": "222"}, clear=True):
            call_llm(request_for("ca_fact"), transport=self.transport)
        self.assertEqual(self.transport.call_args.kwargs["timeout"], 222)

    def test_explicit_timeout_override(self):
        with patch.dict(os.environ, {"YANDEX_TIMEOUT_SECONDS": "999"}, clear=True):
            self.assertEqual(YandexLLMClient(timeout=12, transport=self.transport).timeout, 12)

    def test_no_llm_no_env_no_http(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(call_llm({"llm_required": False}, transport=self.transport), {})
        self.transport.assert_not_called()

    def test_skipped_task_does_not_read_invalid_timeout(self):
        with patch.dict(os.environ, {"YANDEX_TIMEOUT_SECONDS": "invalid"}, clear=True):
            self.assertEqual(call_llm({"llm_required": False}, transport=self.transport), {})
        self.transport.assert_not_called()

    def test_confirmed_context_cannot_be_target(self):
        request = request_for("ca_fact")
        request["confirmed_values"] = {"ca_fact": "Y"}
        with self.assertRaises(InvalidRequestError):
            self.client.call(request)
        self.transport.assert_not_called()

    def test_parser_only_target_rejected(self):
        with self.assertRaises(InvalidRequestError):
            self.client.call(request_for("bpm"))
        self.transport.assert_not_called()

    def test_duplicate_unknown_empty_targets_rejected(self):
        for targets in [("ca_fact", "ca_fact"), ("invented",), ()]:
            with self.subTest(targets=targets), self.assertRaises(InvalidRequestError):
                self.client.call(request_for(*targets))
        self.transport.assert_not_called()

    def test_invalid_json(self):
        self.transport.return_value = io.BytesIO(response_bytes(content="not JSON"))
        with self.assertRaises(InvalidJSONError):
            self.client.call(request_for("ca_fact"))
        self.assertEqual(self.transport.call_count, 1)

    def test_code_fences_rejected(self):
        self.transport.return_value = io.BytesIO(response_bytes(content='```json\n{"ca_fact":"Y"}\n```'))
        with self.assertRaises(InvalidJSONError):
            self.client.call(request_for("ca_fact"))

    def test_duplicate_json_keys_rejected(self):
        self.transport.return_value = io.BytesIO(response_bytes(content='{"ca_fact":"Y","ca_fact":"N"}'))
        with self.assertRaises(InvalidJSONError):
            self.client.call(request_for("ca_fact"))

    def test_nonfinite_json_rejected(self):
        self.transport.return_value = io.BytesIO(response_bytes(content='{"echo_lvd_2":NaN}'))
        with self.assertRaises(InvalidJSONError):
            self.client.call(request_for("echo_lvd_2"))

    def test_missing_target(self):
        with self.assertRaises(MissingFieldsError):
            self.call({}, "ca_fact")

    def test_extra_target(self):
        with self.assertRaises(ExtraFieldsError):
            self.call({"ca_fact": "Y", "bpm": 70}, "ca_fact")

    def test_non_object_response(self):
        with self.assertRaises(UnexpectedResponseError):
            self.call([], "ca_fact")

    def test_wrong_field_type(self):
        with self.assertRaises(SchemaViolationError):
            self.call({"echo_lvd_2": "50"}, "echo_lvd_2")

    def test_wrong_enum(self):
        with self.assertRaises(SchemaViolationError):
            self.call({"ca_fact": "performed"}, "ca_fact")

    def test_bad_binary_object(self):
        for value in [1, {"code": True, "text": "Курит"}, {"code": 1, "text": "Курит", "extra": 0}, {"code": 0, "text": "Нет"}]:
            with self.subTest(value=value), self.assertRaises(SchemaViolationError):
                self.call({"smoking": value}, "smoking")

    def test_binary_allowed_texts_enforced_for_llm(self):
        with self.assertRaises(SchemaViolationError):
            self.call({"smoking": {"code": 1, "text": "invented"}}, "smoking")

    def test_binary_good_objects(self):
        for value in [{"code": 1, "text": "Курит"}, {"code": 0, "text": NOT_SPECIFIED}]:
            self.assertEqual(self.call({"smoking": value}, "smoking")["smoking"], value)

    def test_medication_schema_violations(self):
        for value in [[], 1, None, NOT_SPECIFIED, [{"name": "АСК"}], [{"name": "АСК", "prescription": "100 мг", "extra": 1}], [{"name": " ", "prescription": NOT_SPECIFIED}]]:
            with self.subTest(value=value), self.assertRaises(SchemaViolationError):
                self.call({"aspirin": value}, "aspirin")

    def test_medication_zero_and_list(self):
        for value in [0, [{"name": "АСК", "prescription": NOT_SPECIFIED}]]:
            self.assertEqual(self.call({"aspirin": value}, "aspirin")["aspirin"], value)

    def test_400_no_retry(self):
        self.transport.side_effect = http_error(400)
        with self.assertRaises(LLMHTTPError):
            self.client.call(request_for("ca_fact"))
        self.assertEqual(self.transport.call_count, 1)
        self.sleep.assert_not_called()

    def test_redirect_never_followed_or_retried(self):
        from llm_client import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.example"))
        self.transport.side_effect = http_error(302)
        with self.assertRaises(LLMHTTPError):
            self.client.call(request_for("ca_fact"))
        self.assertEqual(self.transport.call_count, 1)

    def test_authentication_no_retry(self):
        for code in [401, 403]:
            self.transport.reset_mock()
            self.transport.side_effect = http_error(code)
            with self.subTest(code=code), self.assertRaises(AuthenticationError) as caught:
                self.client.call(request_for("ca_fact"))
            self.assertEqual(caught.exception.status_code, code)
            self.assertEqual(self.transport.call_count, 1)

    def test_429_retry(self):
        self.transport.side_effect = [http_error(429), io.BytesIO(response_bytes({"ca_fact": "Y"}))]
        self.assertEqual(self.client.call(request_for("ca_fact")), {"ca_fact": "Y"})
        self.assertEqual(self.transport.call_count, 2)
        self.sleep.assert_called_once_with(0.5)

    def test_5xx_retry_bounded(self):
        self.transport.side_effect = lambda *args, **kwargs: (_ for _ in ()).throw(http_error(503))
        with self.assertRaises(LLMHTTPError):
            self.client.call(request_for("ca_fact"))
        self.assertEqual(self.transport.call_count, 3)
        self.assertEqual([item.args[0] for item in self.sleep.call_args_list], [0.5, 1.0])

    def test_timeout_retry_bounded(self):
        self.transport.side_effect = TimeoutError(TEST_KEY)
        with self.assertRaises(LLMTimeoutError):
            self.client.call(request_for("ca_fact"))
        self.assertEqual(self.transport.call_count, 3)
        for call in self.transport.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 180)

    def test_wrapped_timeout(self):
        self.transport.side_effect = URLError(TimeoutError(TEST_KEY))
        with self.assertRaises(LLMTimeoutError):
            self.client.call(request_for("ca_fact"))
        self.assertEqual(self.transport.call_count, 3)

    def test_connection_reset_retry(self):
        self.transport.side_effect = [ConnectionResetError(TEST_KEY), io.BytesIO(response_bytes({"ca_fact": "Y"}))]
        self.assertEqual(self.client.call(request_for("ca_fact")), {"ca_fact": "Y"})
        self.assertEqual(self.transport.call_count, 2)

    def test_nontemporary_network_error_no_retry(self):
        self.transport.side_effect = URLError("certificate failure " + TEST_KEY)
        with self.assertRaises(LLMNetworkError):
            self.client.call(request_for("ca_fact"))
        self.assertEqual(self.transport.call_count, 1)

    def test_key_not_in_errors_or_traceback(self):
        self.transport.side_effect = http_error(403)
        try:
            self.client.call(request_for("ca_fact"))
        except AuthenticationError:
            rendered = traceback.format_exc()
        self.assertNotIn(TEST_KEY, rendered)

    def test_model_reflected_key_blocked(self):
        with self.assertRaises(UnexpectedResponseError):
            self.call({"echo_lvd_2": TEST_KEY}, "echo_lvd_2")

    def test_phi_in_request_blocks_http(self):
        for phi in ["Пациент: Иванов Иван Иванович", "Ф.И.О.: Иванов Иван Иванович", "Patient: Example Person", "СНИЛС: 123-456-789 00", "Адрес: Москва, улица Тестовая", "mail@example.org"]:
            with self.subTest(phi=phi):
                request = request_for("ca_fact")
                request["user_prompt"] = phi
                with self.assertRaises(PrivacyError) as caught:
                    self.client.call(request)
                self.assertNotIn(phi, str(caught.exception))
        self.transport.assert_not_called()

    def test_phi_in_model_result_blocked(self):
        with self.assertRaises(PrivacyError):
            self.call({"aspirin": [{"name": "АСК", "prescription": "Ф.И.О.: Иванов Иван Иванович"}]}, "aspirin")

    def test_incomplete_generation_rejected(self):
        self.transport.return_value = io.BytesIO(response_bytes({"ca_fact": "Y"}, finish="length"))
        with self.assertRaises(UnexpectedResponseError):
            self.client.call(request_for("ca_fact"))

    def test_oversized_response(self):
        self.transport.return_value = io.BytesIO(b"x" * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(UnexpectedResponseError):
            self.client.call(request_for("ca_fact"))

    def test_explicit_json_mode_still_validates(self):
        client = YandexLLMClient(self.config, response_mode="json_object", transport=self.transport)
        client.call(request_for("ca_fact"))
        self.assertEqual(json.loads(self.transport.call_args.args[0].data)["response_format"], {"type": "json_object"})
        self.transport.return_value = io.BytesIO(response_bytes({"ca_fact": "bad"}))
        with self.assertRaises(SchemaViolationError):
            client.call(request_for("ca_fact"))

    def test_temperature_can_be_omitted(self):
        YandexLLMClient(self.config, temperature=None, transport=self.transport).call(request_for("ca_fact"))
        self.assertNotIn("temperature", json.loads(self.transport.call_args.args[0].data))


class SchemaTests(unittest.TestCase):
    def test_target_only_required_no_extras(self):
        targets = ["ca_fact", "aspirin", "tlt"]
        schema = build_response_schema(targets)
        self.assertEqual(list(schema["properties"]), targets)
        self.assertEqual(schema["required"], targets)
        self.assertFalse(schema["additionalProperties"])

    def test_enum_uses_contract(self):
        schema = build_response_schema(["ca_fact"])
        self.assertEqual(schema["properties"]["ca_fact"]["enum"], FIELD_CONTRACT["ca_fact"]["allowed_values"])

    def test_medication_schema(self):
        schema = build_response_schema(["aspirin"])["properties"]["aspirin"]["anyOf"]
        self.assertEqual(schema[0], {"type": "integer", "enum": [0]})
        self.assertEqual(schema[1]["minItems"], 1)
        item = schema[1]["items"]
        self.assertEqual(set(item["required"]), {"name", "prescription"})
        self.assertFalse(item["additionalProperties"])

    def test_number_or_specific_missing_not_numeric_string(self):
        schema = build_response_schema(["echo_lvd_2"])["properties"]["echo_lvd_2"]["anyOf"]
        self.assertEqual(schema[0], {"type": "number"})
        self.assertEqual(schema[1], {"type": "string", "enum": [NOT_SPECIFIED]})

    def test_builder_does_not_mutate_contract(self):
        before = deepcopy(FIELD_CONTRACT)
        build_response_schema(list(FIELD_CONTRACT))
        self.assertEqual(FIELD_CONTRACT, before)
