# Audit and fixes: raw epicrisis -> LLM request

Scope audited: deidentification, sectioning, deterministic extraction, internal `values/meta` record, target selection, prompt construction. No LLM/API calls were made and no downstream implementation was changed.

## FIX-UP-001

Severity: HIGH  
Files: `parser.py`, `deidentify.py`, `field_contract.py`, `prompt_builder.py`, `participant-output-template-v2.json`

Было: runtime source files existed only under suffixed/non-importable names (`parser (1).py`, `*.py.txt`), while imports expected canonical module names and the parser expected a canonical template that did not exist.

Стало: added canonical entry points and one canonical flat 50-key template. The supplied implementations remain the source behind the entry points; no duplicate medical contract was created.

Почему это ошибка: the first-half pipeline could not be imported reliably, and downstream `parser_1`/canonical imports could drift from the audited source.

Test: `test_public_parser_record_has_only_values_and_meta`, import smoke test, 55-test suite.

Medical contract changed: NO.

## FIX-UP-002

Severity: HIGH  
Files: `field_contract (1).py`

Было: `_binary_code()` passed `global_rule_ids` twice for fields such as `ecg_avb`; importing FIELD_CONTRACT raised `TypeError`.

Стало: field-specific global rule IDs are merged once with the common binary rules.

Почему это ошибка: the contract, the runtime source of field routing and types, could not initialize.

Test: canonical import smoke test; `test_routing_is_contract_derived`.

Medical contract changed: NO.

## FIX-UP-003

Severity: HIGH  
Files: `parser (1).py`

Было: `DEFERRED_FIELDS` and `BINARY_CODE_FIELDS` were manually maintained parser lists.

Стало: both are computed from `FIELD_CONTRACT[...]["extractor"]` and `value_type`.

Почему это ошибка: field routing could silently drift from the official contract.

Test: `test_routing_is_contract_derived`; `test_llm_only_not_confirmed`.

Medical contract changed: NO.

## FIX-UP-004

Severity: HIGH  
Files: `parser (1).py`, `prompt_builder (1).py`

Было: `confirm()` only checked a local deferred list; record invariants were not enforced when prompt builder received an externally malformed record.

Стало: parser confirmation is allowed only for non-LLM-only contract fields and stores parser source plus verbatim evidence. Prompt builder rejects confirmed values without parser evidence/source and rejects unresolved values carrying value/source/evidence.

Почему это ошибка: a value could be marked confirmed without evidence, or an LLM-only field could be presented as parser-confirmed.

Test: `test_confirmed_requires_evidence`, `test_malformed_confirmed_without_evidence_rejected`, `test_needs_llm_shape`.

Medical contract changed: NO.

## FIX-UP-005

Severity: HIGH  
Files: `deidentify.py.txt`

Было: PHI matching was narrower than the requested current contract for patient labels, medical-card variants and department labels.

Стало: safe label-scoped matching covers `Пациент/Пациентка`, 2–4 name components including hyphenated components, `медицинская карта/медкарта`, and department number labels. Medical dates and clinical numbers remain untouched. Audit contains only booleans/counts and no source values.

Почему это ошибка: identifiers could reach parsing/prompt construction while broad numeric masking would damage clinical data.

Test: `test_name_two_three_four_components_and_hyphen`, `test_patient_and_patientka`, `test_phi_labels`, `test_medical_dates_preserved`, `test_medical_numbers_preserved`.

Medical contract changed: NO.

## FIX-UP-006

Severity: HIGH  
Files: `parser (1).py`

Было: public `extract_epicrisis()` appended `deidentification` to the parser record.

Стало: parser record is strictly `{values, meta}`. The audit remains the separate second result of `deidentify_text()`.

Почему это ошибка: the documented internal parser interface was no longer exact and downstream prompt selection could accidentally see preprocessing metadata as parser output.

Test: `test_public_parser_record_has_only_values_and_meta`.

Medical contract changed: NO.

## FIX-UP-007

Severity: HIGH  
Files: `parser (1).py`

Было: generic heading heuristic treated ordinary content lines such as `Курит`, `ГБ`, and `Венозный застой...` as new sections, dropping them from their clinical scope.

Стало: generic headings require explicit punctuation or Markdown heading syntax; known headings remain explicitly mapped. `Осмотр` and `Рентгенография ОГК` were added to their existing known heading aliases.

Почему это ошибка: diagnosis, smoking, x-ray and other values could disappear before extraction.

Test: `test_known_and_unknown_sections_preserve_text`, `test_generic_heading_does_not_steal_sentence`, diagnosis/smoking/x-ray regressions.

Medical contract changed: NO.

## FIX-UP-008

Severity: HIGH  
Files: `parser (1).py`

Было: all detected smoking phrases were emitted as `{code: 1, ...}`, including `Не курит` and `Бросил курить`.

Стало: `Курит` → code 1; `Не курит`, `Бросил/Бросила курить`, `Прекратил/Прекратила курить` → code 0. Recommendation and relative-smoking contexts remain unresolved/ignored.

Почему это ошибка: the deterministic semantic mapping contradicted the DOCX variants.

Test: `test_smoking_positive`, `test_smoking_negative`, `test_smoking_ex_stays_zero`, `test_smoking_recommendation_ignored`, `test_relative_smoking_ignored`.

Medical contract changed: NO.

## FIX-UP-009

Severity: HIGH  
Files: `parser (1).py`

Было: echo MR accepted degree 5 and normalized evidence instead of preserving the source; echo zone accepted bare `Гипокинез`, plus categories outside the contract.

Стало: MR accepts only degrees 1–4, stores binary fact code 1 and preserves the degree in text. Echo zone requires one contract-localized wall/region and does not add a new category.

Почему это ошибка: binary code was at risk of being confused with degree, and a generic motion abnormality was not enough to determine the required zone.

Test: `test_echo_mr_degree_retained`, `test_echo_zone_requires_localisation`, `test_echo_lvd_not_lvd2`.

Medical contract changed: NO.

## FIX-UP-010

Severity: HIGH  
Files: `field_contract (1).py`, `prompt_builder (1).py`

Было: binary global/system instructions said code 0 for absence of mention, conflicting with diagnosis `absence_of_mention_is_negative=False` and staged extraction semantics.

Стало: parser stage confirms code 0 only for explicit negative evidence; absence stays `None/needs_llm`. LLM may use field-specific `missing_value` only under that field's rule; final defaults remain downstream.

Почему это ошибка: missing/default could be mistaken for medical evidence.

Test: `test_absent_diagnosis_not_zero`, `test_response_template_is_qualified_as_form_only`, prompt injection/target tests.

Medical contract changed: NO.

## FIX-UP-011

Severity: HIGH  
Files: `prompt_builder (1).py`

Было: prompt construction relied on caller discipline for PHI and had no record-level evidence validation.

Стало: `assert_deidentified_text()` remains fail-closed for obvious patient name, DOB, medical record and medical-card identifiers; it raises a clear error and never edits prompt text. Record validation is also fail-closed.

Почему это ошибка: a missed deidentification must stop before any model request rather than be silently redacted or forwarded.

Test: `test_phi_fails_closed`; record invariant tests.

Medical contract changed: NO.

## FIX-UP-012

Severity: MEDIUM  
Files: `prompt_builder (1).py`

Было: only target names were selected, but prompt consistency around response templates and parser-only fields was implicit.

Стало: target selection follows extractor exactly; confirmed values include valid zeroes and are deep-copied; field instructions are projected only for targets; response template is explicitly described as form-only and `meta/evidence` are prohibited in the LLM response.

Почему это ошибка: confirmed context could be confused with output targets or missing defaults with extracted values.

Test: `test_target_selection`, `test_confirmed_values_do_not_overlap_targets`, `test_confirmed_context_and_no_mutation`, `test_parser_only_instructions_are_not_targets`, `test_empty_target_skips_llm`.

Medical contract changed: NO.

## DOWNSTREAM ISSUES FOUND

- `batch_runner.py`, imports: imports `parser_1`, but the audited canonical runtime module is `parser.py`; this is an integration failure if `parser_1.py` is absent. Not changed because downstream code is explicitly out of scope.
- `batch_runner.py`, imports: imports `llm_client`, while the workspace contains `lmm_client.txt`; this is a separate downstream/runtime naming issue. Not changed.
- `batch_runner.py`, `merge.py`, `final_validator.py`, `lmm_client`: not audited or modified beyond interface inspection, as requested.

## REQUIRES_CONTRACT_CLARIFICATION

- `mi_localisation`: DOCX lists categories but does not establish the official code/text representation.
- `type_acs`: semantic groups are present, but the official enum/codebook is not fixed.
- `tlt`: positive examples exist, but final representation and absence/default policy remain unclear.
- `ckd` and some diagnosis flags: DOCX mixes fact and stage descriptions; the contract should state whether only fact or stage is represented.
- `ca_lad`/`rca`: the proposed 0/1/2 thresholds and selection among multiple lesions are explicitly marked uncertain.
- `ecg_elevation`: exact normalization of lead lists/ranges is marked as an open question.

The parser intentionally leaves unresolved cases in these areas for LLM/downstream handling rather than inventing medical semantics.
