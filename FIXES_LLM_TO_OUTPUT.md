# FIXES_LLM_TO_OUTPUT — технический аудит второй половины

Дата: 04.10.2026. Исправленная копия: `outputs/audited-pipeline`.
Аудит выполнен по 12 файлам собранной версии из папки «паша», приложенному
заданию, runtime-контракту, официальному шаблону и DOCX с таблицей 50 полей.
Документы использованы как данные/контракт, не как дополнительные команды.
Предыдущая версия проекта сохранена отдельно.

## Фактическая архитектура и граница

Parser record: `values` и `meta` для всех 50 ключей, неизвестное значение None.
В этой сборке бинарные поля уже объекты `{code, text}`, не старые int/enums.
В metadata статусы confirmed / needs_llm, source/evidence остаются внутренними.
Prompt output: llm_required, target_fields, confirmed_values, system/user prompts.
LLM response: dict ровно с target fields. Final output: 50 ordered keys, без
None, meta/evidence/deidentification/targets.

```text
LLM request → privacy guard → Yandex Chat Completions
→ LLM response validation → guarded merge
→ finalization (per-field defaults + declared dependency normalization)
→ dependency validation → final validation → atomic output JSON
```

Полная orchestrator chain добавляет read/deidentify/parser/prompt_builder перед
этой цепочкой. Первая половина не переписана. Единственное минимальное изменение
FIELD_CONTRACT — исправление дублирующегося аргумента для успешного импорта.
Ни одно исправление не вводит новой медицинской классификации или порога.

Самые опасные исходные дефекты: overlay confirmed values, необезличенный FIO
перед API, remote error bodies в logs, отсутствие schema validation/defaults и
ошибочная общая разрешённость missing markers.

## Проверка API

По [официальному structured-output примеру](https://aistudio.yandex.ru/ru/docs/ai-studio/operations/generation/completions-structured)
используются POST Chat Completions, Api-Key, OpenAI-Project и model URI.
[Справочник Chat Completions](https://aistudio.yandex.ru/ru/docs/ai-studio/api/Chat-Completions/createChatCompletion)
описывает json_schema/json_object. API подтверждён по документации, не живым запросом.
Для выбранной модели поддержку strict/subset JSON Schema, temperature и token
limit нужно проверить единичным ручным запуском. После HTTP 400 fallback не
включается автоматически. Permissions аккаунта/исходный HTTP 403 не проверены.

## Исправления

### FIX-001

- **Severity:** CRITICAL
- **File(s):** field_contract.py
- **Было:** Импорт завершался TypeError: _binary_code передавал global_rule_ids одновременно явно и через **details.
- **Стало:** Списки объединены в одном аргументе; описания, типы, defaults и медицинские правила не изменены.
- **Почему это ошибка:** Без импорта контракта downstream и его тесты вообще не запускались.
- **Test:** test_template_contract_order_and_defaults
- **Medical contract changed:** NO

### FIX-002

- **Severity:** HIGH
- **File(s):** batch_runner.py; manual_llm_test.py; канонические имена файлов
- **Было:** Оба runner импортировали отсутствующий parser_1; приложенные runtime-файлы имели суффиксы .txt/(2)/(3).
- **Стало:** В отдельной сборке используются parser.py, llm_client.py и один participant-output-template-v2.json; imports исправлены.
- **Почему это ошибка:** Проект нельзя было запустить в предоставленном составе.
- **Test:** test_mocked_llm_end_to_end_success; test_manual_full_pipeline_mocked
- **Medical contract changed:** NO

### FIX-003

- **Severity:** HIGH
- **File(s):** llm_client.py
- **Было:** Использовались старый Foundation Models endpoint, IAM token, hardcoded model и несовместимый интерфейс manual script.
- **Стало:** Восстановлен отдельный urllib Chat Completions client: API key, OpenAI-Project, system/user messages, model URI из конфигурации.
- **Почему это ошибка:** Транспорт не соответствовал требуемому API и текущему prompt_builder output.
- **Test:** test_headers_endpoint_and_model; test_messages_and_low_temperature
- **Medical contract changed:** NO

### FIX-004

- **Severity:** HIGH
- **File(s):** llm_client.py; .env.example; .gitignore
- **Было:** Конфигурация клиента не совпадала с YANDEX_API_KEY/YANDEX_FOLDER_ID/YANDEX_MODEL; секреты могли читаться через dotenv.
- **Стало:** Три обязательные env variables; проверка URI/каталога/header-safe key; пустой .env.example; реальный .env не создаётся/не читается.
- **Почему это ошибка:** Недостаточная/несогласованная конфигурация обнаруживалась поздно и могла приводить к запросу в неверный каталог.
- **Test:** test_missing_key; test_missing_folder; test_missing_model; test_uri_folder_mismatch; test_bad_key_safe_error
- **Medical contract changed:** NO

### FIX-005

- **Severity:** HIGH
- **File(s):** llm_client.py
- **Было:** В собранном клиенте timeout был 60 секунд и не соответствовал ранее согласованной настройке.
- **Стало:** YANDEX_TIMEOUT_SECONDS, default 180; положительное finite число; timeout передаётся каждой попытке HTTP; explicit override поддержан.
- **Почему это ошибка:** Медленный ответ обрывался преждевременно, а настройка из окружения не работала.
- **Test:** test_default_timeout_180; test_env_timeout_override; test_invalid_timeouts; test_timeout_passed_to_transport
- **Medical contract changed:** NO

### FIX-006

- **Severity:** HIGH
- **File(s):** llm_client.py
- **Было:** Retry охватывал permanent HTTP ошибки и ошибки разбора ответа.
- **Стало:** Не более трёх попыток; retry timeout/429/5xx/временных соединений; 400/401/403/JSON/schema не повторяются; backoff 0.5/1 s.
- **Почему это ошибка:** Повторение неверного запроса не исправляет причину и расходует ресурсы.
- **Test:** test_400_no_retry; test_authentication_no_retry; test_429_retry; test_5xx_retry_bounded; test_timeout_retry_bounded
- **Medical contract changed:** NO

### FIX-007

- **Severity:** CRITICAL
- **File(s):** llm_client.py; pipeline_errors.py
- **Было:** Ошибки включали тело ответа сервера и могли раскрыть отражённый ключ/данные; redirects не контролировались.
- **Стало:** Фиксированные безопасные ошибки без remote body/headers/chained traceback; key исключён из repr и request body; отражённый key блокируется; HTTP redirect запрещён.
- **Почему это ошибка:** Секрет или медицинские данные не должны попадать в logs/stdout/error messages или на другой HTTP host.
- **Test:** test_key_not_in_errors_or_traceback; test_key_hidden_in_config_repr; test_model_reflected_key_blocked; test_redirect_never_followed_or_retried
- **Medical contract changed:** NO

### FIX-008

- **Severity:** HIGH
- **File(s):** llm_client.py; contract_validation.py
- **Было:** Structured output отсутствовал; использовалась только текстовая инструкция вернуть JSON.
- **Стало:** Dynamic JSON Schema только target fields, все required, additionalProperties=false; strict=true. JSON object mode — только explicit option, без автоматического downgrade.
- **Почему это ошибка:** Одна инструкция не ограничивает форму ответа; неизвестные/лишние поля могли пройти дальше.
- **Test:** test_success_structured_response; test_target_only_required_no_extras; test_explicit_json_mode_still_validates
- **Medical contract changed:** NO

### FIX-009

- **Severity:** HIGH
- **File(s):** contract_validation.py
- **Было:** Схема для официальных binary objects не обеспечивала совместный смысл code/text.
- **Стало:** Схема из item_schema: code=0 с точным contract missing text, code=1 с allowed_texts; code — настоящий int 0/1; только code/text.
- **Почему это ошибка:** Нельзя flatten binary object, принимать bool/float либо произвольный code=0 text.
- **Test:** test_bad_binary_object; test_binary_good_objects; test_binary_allowed_texts_enforced_for_llm
- **Medical contract changed:** NO

### FIX-010

- **Severity:** HIGH
- **File(s):** contract_validation.py; final_validator.py
- **Было:** Medication []/extra properties и некорректные состояния принимались существующим validator.
- **Стало:** 0 либо непустой массив; exact name/prescription; обе строки непустые после strip; отсутствие дозы допускает prescription="не указано".
- **Почему это ошибка:** Пустой массив создавал третье состояние; дополнительные ключи нарушали официальный формат.
- **Test:** test_medication_schema; test_medication_schema_violations; test_medication_zero_and_list; test_medication_valid_missing_prescription
- **Medical contract changed:** NO

### FIX-011

- **Severity:** HIGH
- **File(s):** contract_validation.py; requirements.txt
- **Было:** LLM response не проверялся полноценным общим JSON Schema validator.
- **Стало:** Одна трансляция FIELD_CONTRACT и jsonschema Draft 2020-12 для клиента/merge/final validator; dependency явно указана в requirements.
- **Почему это ошибка:** Независимые ручные validators расходятся между собой и с контрактом.
- **Test:** test_wrong_field_type; test_wrong_enum; test_builder_does_not_mutate_contract; test_full_pipeline_with_mocked_http_transport
- **Medical contract changed:** NO

### FIX-012

- **Severity:** HIGH
- **File(s):** llm_client.py; json_io.py
- **Было:** JSON мог извлекаться из Markdown fences; обычный json.loads не обнаруживает duplicate keys/NaN.
- **Стало:** Строгий JSON целиком; duplicate keys/constants запрещены; fences/произвольный текст не вырезаются regex; offline JSON тоже читается строго.
- **Почему это ошибка:** Неоднозначный либо невалидный JSON нельзя считать структурированным результатом.
- **Test:** test_invalid_json; test_code_fences_rejected; test_duplicate_json_keys_rejected; test_nonfinite_json_rejected; test_file_duplicate_keys_and_nan_rejected
- **Medical contract changed:** NO

### FIX-013

- **Severity:** HIGH
- **File(s):** llm_client.py
- **Было:** Не проверялось завершение генерации, единственная choice и корректная Chat Completions envelope.
- **Стало:** Одна choice, finish_reason=stop, string content, отсутствие refusal/tool_calls, bounded response size 2 MiB.
- **Почему это ошибка:** Обрезанный/refused ответ или неожиданная envelope не является успешным извлечением.
- **Test:** test_incomplete_generation_rejected; test_oversized_response; test_non_object_response
- **Medical contract changed:** NO

### FIX-014

- **Severity:** CRITICAL
- **File(s):** privacy_guard.py; llm_client.py; batch_runner.py; manual_llm_test.py
- **Было:** После prompt_builder не было дополнительной защиты от оставшихся явных идентификаторов; Ф.И.О. в train-0002 проходило upstream checks.
- **Стало:** Перед HTTP fail-closed scan, без повторного redaction. Проверяются prompts/context, model result и данные перед сохранением/выводом.
- **Почему это ошибка:** Upstream audit.deidentified=True не доказывает отсутствия PHI; передачу очевидно необезличенного текста нужно остановить.
- **Test:** test_phi_in_request_blocks_http; test_phi_in_model_result_blocked; test_train_2_phi_fails_closed_without_call; test_manual_phi_blocked_before_call_or_print
- **Medical contract changed:** NO

### FIX-015

- **Severity:** CRITICAL
- **File(s):** merge.py
- **Было:** Безусловный overlay мог перезаписать confirmed parser value при ошибке target_fields.
- **Стало:** Каждый target должен иметь meta.status=needs_llm и не иметь route=parser; confirmed target вызывает MergeError.
- **Почему это ошибка:** Подтверждённые факты parser — runtime invariant, а не только текстовая инструкция модели.
- **Test:** test_merge_rejects_confirmed_target; test_merge_preserves_confirmed; test_merge_parser_only_target
- **Medical contract changed:** NO

### FIX-016

- **Severity:** HIGH
- **File(s):** merge.py; llm_client.py
- **Было:** Record проверялся только на одинаковые values/meta keys; target routing/статусы и schema values недостаточно контролировались.
- **Стало:** Record values/meta ровно official set; статусы valid; confirmed не None, needs_llm только None; target unique/known; exact response set/schema до overlay.
- **Почему это ошибка:** Ошибочные upstream records нельзя исправлять незаметным overlay, а неверный LLM result нельзя пропускать.
- **Test:** test_merge_rejects_record_extra_or_missing; test_merge_invalid_status; test_merge_confirmed_none_invalid; test_merge_unconfirmed_non_none_invalid; test_merge_missing_llm_target; test_merge_extra_llm_target
- **Medical contract changed:** NO

### FIX-017

- **Severity:** HIGH
- **File(s):** merge.py; finalization.py
- **Было:** Shallow copy оставлял aliasing вложенных binary/medication objects.
- **Стало:** Результаты merge/defaults получают deepcopy; record/meta, LLM response и FIELD_CONTRACT не мутируют.
- **Почему это ошибка:** Редактирование downstream result не должно менять evidence/source data или глобальные defaults.
- **Test:** test_merge_no_input_mutation_or_aliases; test_finalization_does_not_change_meta_or_contract
- **Medical contract changed:** NO

### FIX-018

- **Severity:** HIGH
- **File(s):** finalization.py; batch_runner.py; merge.py
- **Было:** Отдельного finalization stage не было; unresolved None доходили до output/validator.
- **Стало:** После merge применяется missing_value каждого поля: обычные строки, binary object, medication 0, ca_fact N; parser meta остаётся прежним.
- **Почему это ошибка:** None нормально внутри extraction, но запрещено в submission; единый глобальный default неверен.
- **Test:** test_ordinary_default; test_binary_default; test_medication_default; test_ca_fact_default; test_offline_merge_finalizes_and_validates
- **Medical contract changed:** NO

### FIX-019

- **Severity:** HIGH
- **File(s):** finalization.py; runtime_contract.py; final_validator.py
- **Было:** Validator частично проверял ca_lad/rca, игнорировал ca_date; inapplicable LLM values не нормализовались.
- **Стало:** После defaults читаются declared dependency conditions; при ca_fact!=Y все три поля получают собственные defaults; зависимости повторно валидируются.
- **Почему это ошибка:** Сосудистые значения/дата КАГ без выполненной КАГ неприменимы. Порогов стенозов Python не интерпретирует.
- **Test:** test_ca_fact_n_dependency; test_ca_fact_r_dependency; test_ca_fact_y_dependency; test_conflicting_llm_response_normalized_by_dependency; test_dependencies_rechecked
- **Medical contract changed:** NO

### FIX-020

- **Severity:** HIGH
- **File(s):** final_validator.py; contract_validation.py
- **Было:** Ручные наборы типов расходились с контрактом: echo_lvd_2 считался string.
- **Стало:** Все типы берутся из FIELD_CONTRACT; echo_lvd_2 number. Совместимые публичные wrappers оставлены, второй список типов удалён.
- **Почему это ошибка:** Валидный numeric КСР отвергался, неправильная numeric string могла приниматься.
- **Test:** test_echo_lvd_2_numeric_accepted; test_echo_lvd_2_string_rejected; test_enum_validation
- **Medical contract changed:** NO

### FIX-021

- **Severity:** HIGH
- **File(s):** final_validator.py
- **Было:** Глобальное "не указано" принималось для любого поля.
- **Стало:** Missing допускается только согласно descriptor: ca_fact=N, лекарства=0, binary={code:0,text:...}.
- **Почему это ошибка:** Строковый marker для medication/binary/ca_fact нарушает официальный формат.
- **Test:** test_wrong_missing_markers; test_binary_zero_exact_missing
- **Medical contract changed:** NO

### FIX-022

- **Severity:** HIGH
- **File(s):** contract_validation.py; final_validator.py; json_io.py
- **Было:** isinstance(..., int) пропускал bool, NaN/Infinity не запрещались при проверке/сохранении.
- **Стало:** Bool не number/integer; codes только actual int; строгая JSON serialization allow_nan=False перед проверкой и записью.
- **Почему это ошибка:** Python type quirks и non-finite числа не являются допустимыми clinical measurements.
- **Test:** test_bool_not_number; test_nonfinite_rejected; test_binary_bool_float_wrong_code; test_nonfinite_never_published
- **Medical contract changed:** NO

### FIX-023

- **Severity:** MEDIUM
- **File(s):** contract_validation.py; final_validator.py
- **Было:** Даты проверялись только regex; string patterns могли расходиться с FIELD_CONTRACT.
- **Стало:** Pattern берётся из descriptor; дополнительно datetime.strptime проверяет реальную дату.
- **Почему это ошибка:** 31/02/2025 внешне соответствует формату, но календарно невозможна.
- **Test:** test_impossible_date; test_date_format_from_contract; test_leap_date_accepted; test_string_pattern_validation
- **Medical contract changed:** NO

### FIX-024

- **Severity:** HIGH
- **File(s):** json_io.py; batch_runner.py; merge.py
- **Было:** Output записывался напрямую; offline merge сохранял неfinalized/unvalidated result.
- **Стало:** Publish через temporary в целевом каталоге + fsync/os.replace после finalize/validate; offline merge использует ту же цепочку.
- **Почему это ошибка:** Прерывание записи оставляло частичный JSON, а offline merge обходил final validation.
- **Test:** test_atomic_strict_json; test_failed_atomic_replace_preserves_previous; test_offline_merge_finalizes_and_validates
- **Medical contract changed:** NO

### FIX-025

- **Severity:** HIGH
- **File(s):** batch_runner.py
- **Было:** В orchestration отсутствовали явные llm_validation/finalization stages; ошибки разных стадий выглядели одинаково.
- **Стало:** Полная последовательность стадий; повторная schema validation даже для mocked client; stage/error_type в ProcessingResult и logs; client validation errors классифицируются корректно.
- **Почему это ошибка:** Иначе трудно отличить транспортный сбой от неверного ответа/merge/default/save; invalid response не должен публиковаться.
- **Test:** test_pipeline_errors_report_actual_stage; test_client_validation_error_classified; test_invalid_llm_does_not_write_result; test_full_pipeline_with_mocked_http_transport
- **Medical contract changed:** NO

### FIX-026

- **Severity:** MEDIUM
- **File(s):** batch_runner.py
- **Было:** Ошибка самого логирования могла оборвать обработку; отсутствующий input давал успешный пустой run; malformed request ломал summary.
- **Стало:** Safe failure per document, отдельная log failure; continuation остальных файлов; CLI exit=1 при любых сбоях/пустом setup; summary защищён от malformed upstream request.
- **Почему это ошибка:** Ошибки инфраструктуры не должны маскироваться успехом или отменять обработку независимых документов.
- **Test:** test_batch_continues_after_failed_document; test_batch_nonzero_exit_for_failure; test_log_write_failure_is_reported; test_malformed_upstream_request_returns_failure_without_crashing
- **Medical contract changed:** NO

### FIX-027

- **Severity:** HIGH
- **File(s):** batch_runner.py
- **Было:** Raw exception strings/input filenames попадали в stdout/logs; одинаковые stems .md/.txt перезаписывали output; intermediate сохранялся до privacy check.
- **Стало:** Opaque deterministic SHA-256 file IDs, fixed safe messages, stage/type, logs без prompt/content/header/key; intermediate пишется лишь после privacy/final checks.
- **Почему это ошибка:** Имя файла тоже может содержать имя пациента; collision и ранний intermediate приводят к потере/утечке данных.
- **Test:** test_safe_logs_and_filename; test_batch_same_stem_different_extensions_unique_ids; test_train_2_phi_fails_closed_without_call
- **Medical contract changed:** NO

### FIX-028

- **Severity:** HIGH
- **File(s):** batch_runner.py
- **Было:** Input/output/intermediate/log могли указывать один файл или directory; при alias log писал бы в исходник.
- **Стало:** Resolved paths/каталоги проверяются на равенство; при rejected alias logging отключается, источник не меняется.
- **Почему это ошибка:** Некорректные CLI paths не должны перезаписывать или дописывать raw source.
- **Test:** test_source_output_alias_blocked; test_source_log_alias_does_not_modify_input
- **Medical contract changed:** NO

### FIX-029

- **Severity:** MEDIUM
- **File(s):** manual_llm_test.py
- **Было:** Manual импортировал parser_1, параметры не соответствовали assembled client; полный pipeline не поддерживался.
- **Стало:** Текущие интерфейсы, elapsed time/timeout, validated LLM JSON, optional --full-pipeline с finalize/validate; raw identifiers не выводятся.
- **Почему это ошибка:** Ручной тест должен проверять ту же архитектуру, что и batch, и не обходить privacy/validation.
- **Test:** test_manual_full_pipeline_mocked; test_manual_timeout_reports_elapsed_time; test_manual_phi_blocked_before_call_or_print
- **Medical contract changed:** NO

### FIX-030

- **Severity:** MEDIUM
- **File(s):** llm_client.py; batch_runner.py; manual_llm_test.py
- **Было:** Клиент/runner могли требовать env/network даже при llm_required=False.
- **Стало:** Skipped call возвращает {}; без чтения credentials/timeout и HTTP; orchestration всё равно делает merge/finalize/final validation/save.
- **Почему это ошибка:** Документ без LLM targets не должен зависеть от API configuration, но обязан иметь корректный final JSON.
- **Test:** test_no_llm_no_env_no_http; test_skipped_task_does_not_read_invalid_timeout; test_no_llm_still_valid_final
- **Medical contract changed:** NO

### FIX-031

- **Severity:** MEDIUM
- **File(s):** runtime_contract.py; SOURCE_MANIFEST.json; tests/
- **Было:** Не было общей проверки template/contract names/order/defaults и regression evidence неизменности upstream.
- **Стало:** Runtime exact keys/order/defaults + strict template read; manifest hashes; 128 новых regression tests рядом с тремя предоставленными tests.
- **Почему это ошибка:** Schema/default drift либо незаметное изменение первой половины нарушает разделение ответственности.
- **Test:** test_template_contract_order_and_defaults; test_upstream_template_and_fixtures_unchanged; test_train_confirmed_preserved_after_merge_and_finalize
- **Medical contract changed:** NO

## UPSTREAM ISSUES FOUND

1. **deidentify.py / deidentify_text:** не удаляет `Ф.И.О.` в train-0002,
   также не покрывает адрес/контактные идентификаторы. Audit независимо от
   residual PHI выставляет deidentified=True/raw_identifiers_retained=False.
   **Влияние:** нельзя доверять audit как доказательству безопасности. Наш guard
   блокирует очевидные residual identifiers, включая train-0002, до HTTP/записи.
   Файл upstream оставлен byte-for-byte неизменным.
2. **prompt_builder.py / assert_deidentified_text:** checks повторяют узкий набор
   форм deidentifier и пропускают `Ф.И.О.`. **Влияние:** prompt может быть собран
   с оставшимся именем. Добавлена downstream блокировка, не переписан builder.
3. **parser.py / extract_diagnosis, extract_ecg, extract_echo, extract_xray:**
   binary code=1 text иногда сохраняет evidence span либо короткую категорию,
   а контракт требует формы allowed_texts. Например ecg_rythm="Синусовый",
   echo_zone="Гипокинез", диагнозы с окружающим текстом.
   **Влияние:** strict allowed_texts для финального JSON ломает confirmed values.
   TODO: согласовать upstream normalization/контракт. Сейчас LLM response strict;
   final validator проверяет структуру и непустой code=1 text, сохраняя parser text.
   Самостоятельно медицинские тексты не преобразованы.
4. **parser.py / extract_echo:** regex echo_mr допускает степень 5, allowed_texts
   текущего контракта содержат 1–4; echo_zone принимает Акинез/Дискинез/Аневризма,
   которых нет в нынешнем списке allowed_texts. **Влияние:** тот же TODO;
   downstream не переопределяет подтверждённую степень/категорию.
5. **parser.py / extract_smoking:** все явные статусы включая «Бросил курить»
   получают code=1. DOCX описывает такую запись среди code=0. **Влияние:** это
   медицинская неоднозначность, не техническая ошибка для исправления downstream.
   Существующая выдача не изменена, см. следующий раздел.

Guard — резервная проверка явных меток, не гарантированное обезличивание свободного
текста. Произвольное имя без метки может остаться незамеченным. До обработки
реальных текстов первая половина требует отдельной проверки своего владельца.

## REQUIRES_CONTRACT_CLARIFICATION

- **Binary coding/allowed_texts:** являются ли перечисления исчерпывающими enum
  либо только примерами? Как совместить code=1 и negative descriptions,
  встречающиеся в allowed_texts, с глобальным code=0 → missing text? Особенно
  smoking и rg_pc. Финальная проверка code=1 сейчас ограничена структурой,
  поскольку источник уже confirmed; прямой offline final validator тоже не
  доказывает принадлежность code=1 text к разрешённой медицинской категории.
- **smoking:** DOCX: «Не курит»/«Бросил курить»/«Прекратила курить» относятся к 0;
  parser и allowed_texts binary contract не согласуют это с missing code=0.
  Никакого recoding Python не делает.
- **echo_zone / ecg_rythm / diagnosis flags / echo_mr:** согласовать точную форму
  текста, сохранение зоны/степени и значения, допускаемые parser, но отсутствующие
  в allowed_texts. Нельзя дописать анатомическую зону по догадке.
- **type_acs / mi_localisation / tlt:** DOCX отмечает неполную подтверждённость
  кодировок/критериев и форм. Сейчас используется предоставленный контракт;
  новых классов/правил при конфликтующих формулировках не добавлено.
- **ca_lad / rca:** определить выбор при нескольких поражениях и значение между
  89% и 90%; подтвердить предложенные DOCX thresholds. Существующий контракт
  передаётся модели, Python не округляет проценты и не выбирает сосудистую категорию.
- **diagnosis_icd:** контракт оставляет открытым выбор при нескольких ICD codes;
  downstream не выбирает основной диагноз сам.
- **echo_lvd_2:** описание и актуальная таблица задают numeric КСР ЛЖ, а старое
  open_questions ещё объявляет параметр неуточнённым. Тип number соблюдён;
  отсутствие → field missing_value. Семантика числового 0 остаётся открытой.
- **ecg_elevation / medication groups:** таблица содержит отмеченные как
  неподтверждённые enum/форматы. Runtime оставлен по текущему FIELD_CONTRACT и
  официальному шаблону; не заменён новой классификацией или бинарными medication flags.

## Результаты и воспроизводимость

- Новая собранная версия: **131 tests OK**, из них **128 новых** и **3 исходных**.
- Прежняя версия проекта отдельно: **160 tests OK**, изменений в ней этот аудит
  не вносил. Эти suites не смешаны: форматы binary fields разных версий различаются.
- Исходные 12 предоставленных файлов проверены по SHA-256: ни один не изменён.
  Parser/deidentify/prompt_builder/template/исходные tests в копии тоже неизменны;
  manifest проверяется regression test. Train fixtures не редактировались.
- train-0001: 20 target fields; train-0002: 19, HTTP заблокирован residual FIO;
  train-0003: 17. На train-0001/0003 проверены merge/defaults/validation с mock
  response и сохранение всех confirmed values.
- Full pipeline с mocked HTTP проверен на train-0003. Это техническая проверка,
  не оценка качества медицинского ответа: mock возвращает field missing_values.
- Реальные Yandex requests, массовая обработка и платные calls не выполнялись.
  Реальный API key не читался для запуска, не печатался и не сохранялся; test keys
  являются очевидными фиктивными placeholders. `.env` отсутствует.
- Последовательный runner сохранён. TODO concurrency/rate limiting требует
  реальных квот; max_workers и произвольные лимиты не придуманы.

Команда из каталога исправленной сборки:

```powershell
python -m pip install -r requirements.txt
python -X utf8 -m unittest discover -s tests -v
```

В среде аудита jsonschema установлен в отдельный `work/python-deps`, без изменения
исходников/системного Python. Для воспроизводимости результата здесь использован
jsonschema 4.26.0. Для обычной установки достаточно requirements.txt.

Новые runtime helpers: contract_validation.py, pipeline_errors.py,
privacy_guard.py, runtime_contract.py, finalization.py, json_io.py.
Изменённые runtime modules: llm_client.py, merge.py, final_validator.py,
batch_runner.py, manual_llm_test.py и минимально field_contract.py.
Добавлены README, requirements, .env.example, .gitignore, SOURCE_MANIFEST,
два файла regression tests. Полный перечень содержится в архиве сборки.

Ограничения output: default code=0 не доказывает отсутствия заболевания;
metadata не изменяется. Dependency normalization может сбросить противоречивый
confirmed dependent field при ca_fact!=Y: это применение существующей
applicability rule после merge, не LLM overlay. Более строгую политику конфликтов
upstream можно согласовать отдельно. Старый валидный output сохраняется при
неудачном новом run; статус актуального run смотреть в JSONL logs.
