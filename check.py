#!/usr/bin/env python3
"""Быстрая проверка бота на ошибки без запуска.
Запуск:  python check.py
Ловит: синтаксис, необъявленные имена, забытые импорты, циклы импортов,
путаницу с функцией перевода t, неизвестные ключи и подстановки переводов;
гоняет автотесты из selftest.py."""
import ast, builtins, os, re, sys, importlib

MODS = ["core","i18n","database","spotify","lyrics","helpers","playback","views",
        "events","commands_play","commands_control","commands_misc","bot"]
HEADER = {"discord","commands","app_commands","wavelink","asyncio","os","asyncpg",
          "aiohttp","re","time","base64","logging","datetime","Optional","core"}

def read(m):
    return open(f"{m}.py", encoding="utf-8").read()

def core_all():
    s = read("core")
    m = re.search(r"__all__ = (\[.*\])", s, re.S)
    names = set(eval(m.group(1))) if m else set()
    return names | {"db_pool"}

def check_syntax():
    bad = []
    for m in MODS:
        try: ast.parse(read(m))
        except SyntaxError as e: bad.append(f"{m}.py:{e.lineno}: {e.msg}")
    return bad

def check_names():
    CA = core_all(); BI = set(dir(builtins)); issues=[]
    for m in MODS:
        tree = ast.parse(read(m))
        defined = set(CA)|BI|set(HEADER)
        local = set()
        for node in ast.walk(tree):
            if isinstance(node,(ast.Import,ast.ImportFrom)):
                for a in node.names: defined.add(a.asname or a.name.split(".")[0])
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                defined.add(node.name); local.add(node.name)
            if isinstance(node,ast.Assign):
                for t in node.targets:
                    if isinstance(t,ast.Name): defined.add(t.id)
            if isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name):
                defined.add(node.target.id)
            if isinstance(node,ast.arg): local.add(node.arg)
            if isinstance(node,ast.Name) and isinstance(node.ctx,ast.Store): local.add(node.id)
            if isinstance(node,ast.ExceptHandler) and node.name: local.add(node.name)
            if isinstance(node,(ast.Global,ast.Nonlocal)):
                for n in node.names: defined.add(n)
        for node in ast.walk(tree):
            if isinstance(node,ast.Name) and isinstance(node.ctx,ast.Load):
                if node.id not in defined and node.id not in local:
                    issues.append(f"{m}.py:{node.lineno}: возможно необъявлено '{node.id}'")
    return issues

def check_t_misuse():
    """В модулях, где t — функция перевода, t можно только вызывать: t(...).
    Так ловится t.length / t["title"] после переименования переменной цикла
    (из-за этого падали /queue и «Сменить версию») и переменная с именем t."""
    issues = []
    for m in MODS:
        tree = ast.parse(read(m))
        if not any(isinstance(n, ast.ImportFrom) and n.module == "i18n"
                   and any(a.name == "t" and not a.asname for a in n.names)
                   for n in ast.walk(tree)):
            continue
        called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        for node in ast.walk(tree):
            if isinstance(node, ast.arg) and node.arg == "t":
                issues.append(f"{m}.py:{node.lineno}: аргумент назван t — перекрывает функцию перевода")
            elif isinstance(node, ast.Name) and node.id == "t":
                if isinstance(node.ctx, ast.Store):
                    issues.append(f"{m}.py:{node.lineno}: переменная t перекрывает функцию перевода")
                elif id(node) not in called:
                    issues.append(f"{m}.py:{node.lineno}: t используется не как вызов t(...) — "
                                  f"скорее всего, имелась в виду другая переменная")
    return issues

def load_strings():
    for node in ast.parse(read("i18n")).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "STRINGS"):
            return ast.literal_eval(node.value)
    return {}

def placeholders(text):
    return set(re.findall(r"\{(\w+)\}", text))

def check_i18n():
    """Ключи t(...) есть в STRINGS, у каждого есть ru и en, и вызов передаёт
    подстановки обоих языков (иначе вместо текста виден шаблон «{title}»).
    Подстановки в ru и en могут различаться: «{type_cap} недоступен» /
    «This {type_low} is unavailable» — поэтому сверяем вызов с их объединением."""
    strings = load_strings(); issues = []
    for key, entry in strings.items():
        if not entry.get("ru") or not entry.get("en"):
            issues.append(f"i18n: у ключа {key!r} нет перевода ru или en")
    for m in MODS:
        for node in ast.walk(ast.parse(read(m))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "t" and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                continue
            key = node.args[1].value
            if key not in strings:
                issues.append(f"{m}.py:{node.lineno}: неизвестный ключ перевода {key!r}")
                continue
            if any(kw.arg is None for kw in node.keywords):
                continue  # t(..., **kwargs) — не проверить
            need = placeholders(strings[key].get("ru", "")) | placeholders(strings[key].get("en", ""))
            missing = need - {kw.arg for kw in node.keywords}
            if missing:
                issues.append(f"{m}.py:{node.lineno}: {key!r} ждёт {sorted(missing)}, а их не передали")
    return issues

def check_imports():
    os.environ.setdefault("DISCORD_TOKEN","dummy")
    try:
        importlib.import_module("bot"); return []
    except Exception as e:
        return [f"Ошибка импорта: {type(e).__name__}: {e}"]

def check_selftest():
    os.environ.setdefault("DISCORD_TOKEN","dummy")
    try:
        importlib.import_module("bot")
        return importlib.import_module("selftest").run()
    except Exception as e:
        return [f"Автотесты не запустились: {type(e).__name__}: {e}"]


def check_help_coverage():
    """Мягкая проверка: все ли команды упомянуты в /help (HELP_CATEGORIES)."""
    os.environ.setdefault("DISCORD_TOKEN", "dummy")
    try:
        importlib.import_module("bot")
        cm = importlib.import_module("commands_misc")
        botmod = importlib.import_module("bot")
        help_text = " ".join(c for _, _, c in cm.HELP_CATEGORIES.values())
        missing = []
        for c in botmod.tree.walk_commands():
            name = c.qualified_name
            if name == "help":
                continue
            leaf = name.split()[-1]
            if f"/{name}" not in help_text and leaf not in help_text:
                missing.append(name)
        return sorted(missing)
    except Exception as e:
        return [f"(не удалось проверить: {e})"]

print("=== Surge: быстрая проверка ===\n")
ok = True
for label, fn in [("Синтаксис", check_syntax),
                  ("Необъявленные имена", check_names),
                  ("Функция перевода t", check_t_misuse),
                  ("Переводы и подстановки", check_i18n),
                  ("Импорт всех модулей", check_imports),
                  ("Автотесты", check_selftest)]:
    res = fn()
    if res:
        ok = False
        print(f"✗ {label}: {len(res)} проблем")
        for r in res[:20]: print(f"    {r}")
    else:
        print(f"✓ {label}: чисто")
missing_help = check_help_coverage()
if missing_help:
    print("\n⚠️  В /help, возможно, не хватает команд (не блокирует деплой):")
    for m in missing_help:
        print(f"    {m}")
else:
    print("\n✓ /help покрывает все команды")

print("\n" + ("✅ ВСЁ ЧИСТО — можно коммитить" if ok else "⚠️ Есть проблемы — см. выше"))
sys.exit(0 if ok else 1)
