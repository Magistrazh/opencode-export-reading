# opencode-export-reading

[English](README.md)

[![skills.sh](https://img.shields.io/badge/skills.sh-listed-blue)](https://skills.sh/magistrazh/opencode-export-reading)

При компакции OpenCode в текущем контексте остаётся сводка, а сообщения до
`tail_start_id` исчезают. В экспорте сессии они сохраняются. Этот инструмент
читает экспорт локально и показывает сообщения, выпавшие из контекста, вместе
с их id.

Это консольная утилита на Python, она же оформлена как скилл OpenCode.
Сторонних зависимостей нет.

## Установка

Нужен Python 3.10+.

Самый быстрый способ — skills CLI:

```bash
npx skills add Magistrazh/opencode-export-reading
```

Установка из клона:

```bash
git clone https://github.com/Magistrazh/opencode-export-reading.git
cd opencode-export-reading

# Linux / macOS
mkdir -p ~/.config/opencode/skills
ln -s "$PWD/skills/opencode-export-reading" ~/.config/opencode/skills/opencode-export-reading
```

На Windows создай каталог скиллов, затем скопируй каталог скилла или создай
символическую ссылку в режиме разработчика:

```powershell
New-Item -ItemType Directory -Force -Path "$HOME\.config\opencode\skills" | Out-Null
New-Item -ItemType SymbolicLink `
  -Path "$HOME\.config\opencode\skills\opencode-export-reading" `
  -Target (Join-Path (Get-Location) "skills\opencode-export-reading")
```

Сам скилл лежит в `skills/opencode-export-reading/`; в корне репозитория —
документация, evals и CI.

После изменения каталога скиллов перезапусти OpenCode. Примеры команд — для
Bash; в Windows запускай их в Git Bash или используй `$env:SKILL_DIR` в
PowerShell. Проверка установленной копии:

```bash
SKILL_DIR=~/.config/opencode/skills/opencode-export-reading
python "$SKILL_DIR/scripts/inspect-export.py" --selftest
```

## Использование

Выгрузи сессию в файл и передай его инструменту:

```bash
opencode export <session-id> > session.json

SKILL_DIR=~/.config/opencode/skills/opencode-export-reading
EXPORT=/path/to/session.json
```

Начни с карты компакций и сводки, которая осталась в контексте:

```bash
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --compactions
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --show-summary 2
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --summary-outline 2
```

Если `--compactions` ничего не выводит, компакций в сессии не было.

Поиск по истории:

```bash
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --search "решение" --before-tail
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --search "решение" --from-index 100 --to-index 300
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --search "0\.2\.1-rc" --regex --before-tail
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --queries "правило,решение,отклонённый вариант" --before-tail
```

`--before-tail` оставляет только сообщения, выпавшие после последней
компакции; `--from-index`/`--to-index` ограничивают поиск включительным
диапазоном индексов сообщений. `--queries` ищет точные строки; несколько regex-шаблонов задаются
через `--queries-file`.

Если одного попадания недостаточно, посмотри соседние сообщения:

```bash
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --dump --from-index 209 --count 4
python "$SKILL_DIR/scripts/inspect-export.py" "$EXPORT" --around 1258 --window 2
```

Для `--dump` нужен `--count` или начало (`--from-index`/`--from-id`) вместе
с `--count`, `--to-*` или `--all`. `--only text` оставляет только текст. Флаг `--json` доступен для overview, timeline,
compactions, поиска, сводок и `--list-exports`.

## Почему не обычный grep

Экспорт может занимать десятки мегабайт. `grep` выдаёт отдельные строки без id
сообщения и роли, а открывать файл в редакторе дольше, чем читать его потоком.
Скрипт делает короткий предварительный проход по границам компакций, затем
один потоковый проход по сообщениям, оставляет не больше одного попадания на
сообщение и показывает точное число совпадений.

## Локальные данные

Каждый запуск пишет небольшую запись об использовании в
`~/.local/share/opencode/skill-feedback/opencode-export-reading/usage/`: режим,
имя файла, размер, длительность, версия скилла, необязательный opaque id
сессии, хеш `SKILL.md` и счётчики результата. Текст запросов и содержимое
транскрипта туда не попадают. Каталог можно изменить через
`SKILL_FEEDBACK_DIR`. Отзывы, которые пишет `feedback.py`, необязательны и тоже
остаются локальными. Сообщения CLI пока на русском.

Триаж мейнтейнера (`feedback.py --open`, `--mark`) — append-only журнал в
подкаталоге `triage/` рядом с записями: побеждает последнее событие по записи,
сами записи никогда не перезаписываются.

## Ограничения

Инструмент только читает экспорт. Он не возвращает сообщения в текущую сессию
и не меняет файл. Если `tail_start_id` отсутствует или не удаётся его
определить, скрипт завершается ошибкой, а не угадывает границу.

В экспорте могут быть ключи, личные данные и текст с prompt injection. Такие
файлы лучше не выносить в issues, чаты или отчёты с замерами.

## Тесты

```bash
python skills/opencode-export-reading/scripts/inspect-export.py --selftest
python skills/opencode-export-reading/scripts/feedback.py --selftest
python scripts/validate.py --selftest
python evals/opencode-export-reading/run-golden.py
```

Проверка trigger-корпуса без вызова модели:

```bash
bash evals/opencode-export-reading/run-trigger.sh --dry-run
```

Живой прогон требует установленного/связанного скилла и обращается к модели.
Он стоит денег и в CI не входит:

```bash
STAMP=local RUNS=2 bash evals/opencode-export-reading/run-trigger.sh
```

## Файлы

- `skills/opencode-export-reading/scripts/inspect-export.py` — парсер экспорта и CLI
- `skills/opencode-export-reading/scripts/feedback.py` — локальные записи отзывов
- `scripts/validate.py` — проверка структуры репозитория
- `skills/opencode-export-reading/references/schema.md` — поля экспорта, которые использует парсер
- [CHANGES.md](CHANGES.md) — история релизов
- [evals/opencode-export-reading/README.md](evals/opencode-export-reading/README.md) — набор проверок evals

## Как участвовать

См. [CONTRIBUTING.md](CONTRIBUTING.md). Вопросы безопасности:
[SECURITY.md](SECURITY.md).

## Лицензия

MIT. См. [LICENSE](LICENSE).
