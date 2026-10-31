# mcplens

[English](README.md) | [Русский](README.ru.md)

Скилл и CLI, которые отлаживают **самих ИИ-агентов**: прозрачный stdio-прокси MCP с записью, и офлайн-аудит петель в трейсах и сохранённых транскриптах сессий (Claude Code, Codex).

Когда агент ведёт себя плохо, все спорят наощупь — «он всё время зовёт один и тот же инструмент». `mcplens` заменяет спор записью:

```text
$ mcplens analyze session.ndjson
[WARN] repeated-calls (search) L6-L16
        search called 6 times with identical arguments ({"query": "config.toml"}…)
[WARN] ping-pong (search) L19-L25
        search <-> read_file alternated 4 times without progress

$ mcplens session ~/.claude/projects/myproject/<uuid>.jsonl
format   claude   tokens in ~757,108,608 out ~6,627,668
[WARN] tool-error (Bash) L19-L16522
        Bash returned an error 35 time(s); e.g. Exit code 1 exists=False …
```

## Что в репозитории

- **skills/agent-wire-audit** — процесс отладки: запись MCP-трафика через drop-in прокси (обе эры протокола проходят нетронутыми — framing это построчный JSON-RPC), аудит сохранённых транскриптов (стриминг: 100+ МБ за секунды) и чтение находок как инженер — каждый паттерн имеет причину и фикс, а фикс доказывается трейсом до/после, не обещанием.
- **tools/mcplens** — CLI: `proxy` (drop-in stdio-рекордер), `analyze` (repeat-calls, ping-pong, unanswered, сгруппированные tool-errors по трейсу), `session` (тот же движок по JSONL-транскриптам Claude Code / Codex с суммами токенов), `demo` (синтетический трейс зацикленного агента), `--json`, exit 1 при warn-находках, только stdlib.

## Установка

```bash
pip install mcplens
npx skills add juzorei/mcplens
```

Drop-in конфиг прокси для любого MCP-клиента:

```jsonc
{ "mcpServers": { "myserver": {
    "command": "mcplens",
    "args": ["proxy", "--out", "session.ndjson", "--", "python", "server.py"]
}}}
```

## Ещё из портфеля

Один из серии прицельных наборов «скилл + CLI» одного автора:

- [loglens](https://github.com/juzorei/loglens) — триаж больших логов тем же способом
- [ci-autopsy](https://github.com/juzorei/ci-autopsy) — классифицируй упавшие CI-прогоны до перезапуска
- [skilldoctor](https://github.com/juzorei/skilldoctor) — держи установленный набор скиллов здоровым

Остальные — в [профиле juzorei](https://github.com/juzorei?tab=repositories).

## Рамки

Трейсь своих агентов и MCP-серверы; аудитируй транскрипты, которыми владеешь или имеешь право читать.

Лицензия MIT.
