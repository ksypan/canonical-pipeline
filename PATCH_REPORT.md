# Уточнения кейсодателя — точечный patch текущего canonical-pipeline

Дата: 04.10.2026. Работа выполнена непосредственно в текущей папке
Desktop/hac/canonical-pipeline; новый pipeline, repository или ZIP не создавались.

## Что изменено

- field_contract.py / field_contract_impl.py: экспорт NOT_SPECIFIED = «Не указано»;
  формат DD.MM.YYYY, версия 2.1-case-clarifications; явные MI/ICD, coronary и
  X-ray rules. Специализированные binary/medication/ca_fact defaults сохранены.
- participant-output-template-v2.json: тот же единственный список 50 ключей
  и структур; строковые markers приведены к «Не указано» через FIELD_CONTRACT.
- parser_impl.py: output/calendar normalization дат с точками; binary missing
  использует NOT_SPECIFIED. Обычная клиническая строка с точкой в конце больше
  не превращается в неизвестный заголовок; известные dot headings сохранены.
- contract_validation.py: календарная проверка нового формата, единый marker;
  отдельная строгая схема internal values/evidence envelope и его разделение.
- prompt_builder_impl.py: opt-in service envelope; target-only цитаты, null
  для missing без подтверждения. Field instructions содержат новые правила.
- llm_client.py: structured output поддерживает envelope и прежний flat mode;
  проверяются и официальные значения, и форма evidence map. Credentials,
  endpoint, timeouts/retries и privacy architecture не менялись.
- clinical_rules.py (новый): централизованные ICD mapping и vessel-scoped
  thresholds/no-significant-stenosis rules; никакого округления.
- evidence_layer.py (новый): source/scope/value проверки цитат; deterministic
  text-first MI/ICD и coronary/X-ray consistency после merge; защита confirmed
  parser values; evidence отбирается после defaults/dependencies.
- batch_runner.py / manual_llm_test.py: service envelope включён в основные
  пути; result/evidence/meta доступны backend; --service-dir позволяет отдельно
  сохранить service JSON. Журналы не содержат clinical values/citations.
- tests/test_input_to_llm.py, tests/test_llm_client.py,
  tests/test_output_pipeline.py, tests/test_merge_integration.py адаптированы;
  tests/test_case_clarifications.py добавлен. README/.gitignore обновлены.
- SOURCE_MANIFEST.json / MERGE_MANIFEST.json обновлены для patch. Предыдущие
  манифесты сохранены в references/MERGE_SOURCE_MANIFEST.json и
  references/ORIGINAL_MERGE_MANIFEST.json; исходные fixtures/DOCX/FIXES неизменны.

## Точные форматы и ICD mapping

Даты: **DD.MM.YYYY**. Input DD/MM/YYYY и DD.MM.YYYY принимается parser;
финальный DD/MM/YYYY и невозможный 31.02.2025 validator отклоняет.
Missing marker: **«Не указано»** (одна runtime-константа NOT_SPECIFIED).
Binary missing = {code: 0, text: "Не указано"}; medication missing = 0;
ca_fact missing = "N". Parser неизвестное по-прежнему хранит None/needs_llm.

| Точный ICD | mi_localisation output |
| --- | --- |
| I21.0 | {code: 1, text: "передней стенки"} |
| I21.1 | {code: 1, text: "нижней стенки"} |

Основание: существующие allowed_texts и заголовки кодов в
[ICD-10 NHS](https://classbrowser.nhs.uk/ICD-10-5TH-Edition/vol1/block-i20-i25.htm).
Таблица намеренно ограничена двумя широкими категориями. Совпадение точное:
I21.10 не трактуется как I21.1. Приоритет — явный диагноз → подтверждённый
diagnosis_icd из того же diagnosis source → field missing_value. Нет fallback
из ECG/CA, из неоднозначного/отрицаемого диагноза или unsupported ICD.

## Evidence и нерегулярные формулировки

Official result содержит только 50 полей. Service evidence — отдельная map
field → короткая verbatim строка. До merge проверяются exact substring,
раздел и консервативное соответствие значению; выдуманная/неподходящая цитата
исключается, имя поля остаётся в rejected_evidence_fields. Missing без цитаты
не получает evidence. Parser meta.evidence сохраняется для surviving confirmed
values; ICD fallback цитирует исходный ICD и отмечает provenance icd_fallback.
Backend получает ProcessingResult.result/evidence/meta или to_service_result().

Правила зафиксированы в FIELD_CONTRACT, автоматически передаются в prompts,
исполняются в clinical_rules/evidence_layer и описаны в README:

- Для конкретного LAD/ПМЖВ или RCA/ПКА «гемодинамически значимого стеноза нет»
  → 0; LLM 2 исправляется по источнику при ca_fact = Y.
- 0 <50%, 1 =50–89%, 2 ≥90%/окклюзия; 89.5%, разные стенозы, диапазоны и
  противоречия не округляются и не угадываются.
- Explicit negative venous/pulmonary congestion/edema → rg_pc code=0
  с исходной цитатой. Отсутствие упоминания не является доказательством.
- Synthetic evaluation data не требует anonymization по кейсу;
  deidentify/privacy guard сохранён для возможных реальных медицинских данных.

## Проверки

**232 теста проходят:** исходные 190 + 42 новых, включая subtests для ICD,
порогов, отрицаний и разных сосудов. Протокол: TEST_RESULTS.txt.
Команда: python -X utf8 run_tests_offline.py. Python 3.14.6, существующий .venv.
Сетевые соединения/DNS заблокированы; HTTP/LLM mock. Реальных API calls после
patch: **0**. Учтён сообщённый пользователем успешный live run до изменений;
новая service response schema в реальном Yandex вызове ещё не проверялась.

## Оставшиеся CONTRACT_CLARIFICATION

- I21.2 охватывает несколько разных стенок; I21.3/I21.9 не задают стенку;
  I21.4 описывает тип инфаркта, а не локализацию. Остальные ICD вне таблицы
  и сочетанные/неподдерживаемые текстовые локализации требуют уточнения.
- Официальный codebook/представление mi_localisation и type_acs из прежнего
  DOCX не подтверждены; существующая binary структура сохранена по запросу.
- Детали документирования ТЛТ, fact vs stage ХБП/диагнозов и нормализация
  ECG lead lists сохраняют прежние открытые вопросы.
- Выбор нескольких разных stenoses, проценты между 89 и 90 и диапазоны не
  имеют утверждённого правила; возвращается field missing_value.
- Для parser code=1 по-прежнему сохранена audited совместимость текстов,
  не всегда входящих в allowed_texts. LLM texts проверяются строго.

Исторические FIXES/MERGE_REPORT/DOCX не редактировались: текущие уточнения
описаны здесь и в README и имеют приоритет при запуске этой сборки.
