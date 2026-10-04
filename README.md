# Единый medical extraction pipeline

Свежая первая половина объединена с downstream из audited-pipeline.zip.
Подробности: [MERGE_REPORT.md](MERGE_REPORT.md). Оба исходных FIXES-отчёта сохранены.

```text
read → deidentify → parser {values, meta} → prompt_builder → privacy guard
→ LLM {values, evidence} → response/evidence validation → merge
→ clinical consistency (MI/ICD, coronary, X-ray) → finalization/defaults/dependencies
→ final validation → atomic JSON save (ровно 50 полей)
```

## Установка

Python 3.10+. Актуальный patch проверен на Python 3.14.6 в существующем .venv.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Тесты без API

```powershell
python -X utf8 run_tests_offline.py
```

232 теста: исходные 190 адаптированы, добавлены 42 проверки уточнений кейса.
HTTP/LLM в тестах подставные; offline runner запрещает сетевые соединения и DNS.
Ключи Yandex для тестов не нужны. Доступен и стандартный запуск:

```powershell
python -X utf8 -m unittest discover -s tests -v
```

## Рабочий запуск

Положите текстовые документы .txt/.md в отдельную папку input.
Задайте YANDEX_API_KEY, YANDEX_FOLDER_ID, YANDEX_MODEL в окружении процесса.
Названия переменных перечислены в .env.example; сам .env автоматически не читается.

```powershell
python -X utf8 batch_runner.py --input-dir input --output-dir result --intermediate-dir intermediate --log-dir logs --service-dir service
```

Рабочий запуск обращается к Yandex, если нужны поля LLM.
В рамках объединения реальные запросы не выполнялись.

Из backend можно вызвать batch_runner.process_single_file(input_file, output_file, ...)
или process_all_files(...). ProcessingResult сообщает успех, этап ошибки,
число целевых полей и время. Его result/evidence/meta и метод to_service_result()
доступны backend. Имена результатов/журналов используют непрозрачные ID.
--service-dir необязателен: он сохраняет отдельные .service.json для демонстрации.
Официальные файлы в result по-прежнему содержат ровно 50 ключей.

Шаблон participant-output-template-v2.json единственный; его ключи, порядок
и missing values совпадают со свежим FIELD_CONTRACT. Путь к шаблону вычисляется
относительно модуля. Пользовательские относительные пути ввода/вывода вычисляются
относительно текущего каталога; из другой папки передайте абсолютные пути.

## Принятые правила для неоднозначных формулировок

- Даты финального результата: **DD.MM.YYYY**. Входные даты с точками или
  слешами принимаются; невозможные календарные даты отклоняются.
- Единственный строковый missing marker — **«Не указано»**, константа
  NOT_SPECIFIED из FIELD_CONTRACT. Неизвестное в parser record остаётся None.
  Специализированные missing values сохранены: binary = {code: 0, text: "Не указано"},
  medication = 0, ca_fact = "N". Пропуск prescription = "Не указано".
- Для конкретного LAD/ПМЖВ или RCA/ПКА фраза «гемодинамически значимого
  стеноза нет» означает категорию **0**. Post-LLM consistency исправляет
  противоречащий ответ модели по исходному разделу КАГ; другой сосуд
  не заимствует эту формулировку. Применимость ca_fact = Y сохранена.
- Пороги сохранены: 0 — <50%; 1 — 50–89%; 2 — ≥90% или явная окклюзия.
  Значения 89–90%, диапазоны, противоречия и выбор между несколькими
  разными стенозами остаются unresolved/missing; округления нет.
- Явное отрицание венозного/лёгочного застоя или отёка в рентгенографии
  означает rg_pc code = **0**, с цитатой отрицания. Молчание не считается
  цитатой-доказательством; отсутствие данных получает field missing_value.
- Локализация ИМ: явный диагноз имеет приоритет, затем точный ICD fallback,
  затем field missing_value. ECG и анатомия сосудов не используются.
  Единственная таблица в clinical_rules.ICD_MI_LOCALISATION:
  I21.0 → «передней стенки»; I21.1 → «нижней стенки».
  Результат сохраняет binary-структуру {code: 1, text: category}.
  Основание сопоставления — заголовки I21.0/I21.1 в
  [ICD-10 NHS](https://classbrowser.nhs.uk/ICD-10-5TH-Edition/vol1/block-i20-i25.htm)
  и существующие allowed_texts проекта. I21.2, I21.3, I21.4, I21.9 и
  любые неподдерживаемые коды не классифицируются без явного текста.
  CONTRACT_CLARIFICATION попадает в service meta.clarifications.
- Synthetic evaluation data по кейсу не требует anonymization. Privacy layer
  сохранён для возможного использования с реальными медицинскими данными:
  deidentify и fail-closed проверки перед API/сохранением продолжают работать.

## Service evidence layer

Batch/manual запрашивают внутренний structured response {values, evidence}:
values содержит только target fields, evidence — те же ключи со строкой-цитатой
до 240 символов либо null. Перед merge envelope отделяется от official values.
Цитата принимается только как точный substring обезличенного исходного текста,
из соответствующего раздела, с консервативной проверкой соответствия значению.
Выдуманные, неподходящие и неподтверждённые цитаты исключаются; имена их полей
сохраняются в meta.rejected_evidence_fields. Для missing без явной цитаты
evidence отсутствует. Значение модели без принятой цитаты не получает доказательство.

Parser quotes берутся из record.meta[field].evidence, сохраняются для неизменных
confirmed values и не перезаписываются LLM. Для ICD fallback цитируется исходный
код диагноза; meta.evidence_sources указывает icd_fallback, diagnosis_text,
coronary_text, xray_text, parser или llm. После defaults/dependencies удаляются
цитаты значений, которые не вошли в финальный результат.

Backend получает {success, result, evidence, meta, error, stage} через
ProcessingResult.to_service_result(); result — отдельный объект ровно из 50 полей.
--service-dir сохраняет {success, result, evidence, meta} отдельно от result JSON.
Клинические значения, цитаты и service meta не включаются в журналы обработки.
manual_llm_test.py --full-pipeline печатает service evidence отдельно от final JSON.

Для старых прямых consumers build_llm_request/call_llm без include_evidence=True
сохранён прежний flat values response. Основные batch/manual пути используют
новый service envelope; общий merge_record по-прежнему принимает только values.

## Оставшиеся CONTRACT_CLARIFICATION

Остаются неоднозначные ICD-коды/сочетанные локализации, официальный codebook
type_acs/mi_localisation, детали ТЛТ/стадий ХБП, нормализация ECG lead lists,
несколько стенозов и промежуток 89–90%. Новые уточнения имеют приоритет над
историческими FIXES/MERGE_REPORT и DOCX; эти исходные документы не переписаны.
Полный актуальный перечень и изменённые файлы: [PATCH_REPORT.md](PATCH_REPORT.md).
Оставшийся Ф.И.О. в train-0002 блокируется privacy guard до API.
LLM проверяется строго; для финальных parser values сохранена совместимость
текста code=1 из downstream-аудита и добавлена узкая совместимость
отрицательных code=0 формулировок smoking свежего parser.
Пользователь сообщил об успешном реальном full-pipeline run до этого patch.
После patch проверялись только mocked HTTP/LLM, с запрещёнными сетевыми
соединениями; реальный новый values/evidence envelope с Yandex не проверялся.

