# Merge report — 04.10.2026

Собран единый запускаемый pipeline. **190 тестов OK**, реальные Yandex API calls
не выполнялись; повторный медицинский аудит не проводился.

## Источники и приоритеты

- Свежие вложения parser.py, parser_impl.py, field_contract.py,
  field_contract_impl.py, deidentify.py, prompt_builder.py скопированы без
  изменения содержимого; суффиксы имён убраны в рабочей копии.
- Downstream и его 131 тест — из audited-pipeline.zip. Оба FIXES-отчёта,
  DOCX-справочник и train fixtures сохранены.
- Вложения не содержали deidentify_impl.py, prompt_builder_impl.py и тесты
  первой половины. deidentify_impl.py восстановлен без изменения содержимого
  из локального Downloads/Telegram Desktop/deidentify.py (2).txt;
  55 upstream-тестов взяты из локального test_input_to_llm.py.
  prompt_builder_impl.py восстановлен из локального prompt_builder.py:
  по свежему FIXES_INPUT_TO_LLM.md возвращены проверки parser source/evidence,
  запрет source/evidence для unresolved и staged binary instruction.
  **Это реконструкция по отчёту и тестам, а не побайтовая копия отсутствующего
  свежего impl-файла.**

## Конфликты и минимальные изменения

1. Единственный participant-output-template-v2.json взят из audited-архива:
   все 50 ключей, порядок и defaults точно совпадают со свежим FIELD_CONTRACT.
   Приложенный (5).json содержал 15 несовместимых defaults: скаляры вместо
   объектов code/text. Медицинский контракт не менялся.
2. Из batch/manual убрано добавление record['deidentification'].
   В prompt builder передаётся ровно {values, meta}; audit остаётся отдельно.
3. Финальная schema принимает подтверждённые parser значения smoking:
   code=0 и «Не курит», «Бросил/Бросила курить», «Прекратил/Прекратила курить».
   Строгая LLM-схема не ослаблена; code=0 с «Курит» отклоняется.
4. Runtime-имена канонические, legacy parser_1/lmm_client отсутствуют.
   Impl-модули находятся рядом с обёртками; пути к одному шаблону локальны модулю.
5. Проверка исходных хэшей обновлена для выбранных upstream-файлов.
   AUDITED_SOURCE_MANIFEST.json сохраняет первоначальный манифест,
   SOURCE_MANIFEST.json — выбранные upstream и неизменные fixtures,
   MERGE_MANIFEST.json — источники и хэши всей сборки.
   Старый README сохранён в references/AUDITED_README.md.

## Проверки и ограничения

55 upstream + 131 audited + 4 интеграционных = **190 OK**.
Проверен полный проход с mocked HTTP и сохранением 50 полей; новые тесты
проверяют пять отрицательных форм smoking, точный parser record, запрет
code=0/«Курит», неверного confirmed source и unresolved evidence.
Команда: python -X utf8 run_tests_offline.py. Сетевые соединения/DNS заблокированы;
ключи не использовались. Протокол: TEST_RESULTS.txt. Python 3.12.14,
зависимости из существующего локального окружения, без сетевой установки.

REQUIRES_CONTRACT_CLARIFICATION обоих аудитов сохранены.
Train-0002 с Ф.И.О. блокируется до API; реальный сервис/модель Yandex не проверены.
Исходные файлы проекта и вложения не перезаписывались.

