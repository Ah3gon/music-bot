#!/usr/bin/env python3
"""Быстрая проверка бота на ошибки без запуска.
Запуск:  python check.py
Ловит: синтаксис, необъявленные имена, забытые импорты, циклы импортов."""
import ast, builtins, os, re, sys, importlib

MODS = ["core","database","spotify","lyrics","helpers","playback","views",
        "events","commands_play","commands_control","commands_misc","bot"]
HEADER = {"discord","commands","app_commands","wavelink","asyncio","os","asyncpg",
          "aiohttp","re","time","base64","logging","datetime","Optional","core"}

def core_all():
    s = open("core.py").read()
    m = re.search(r"__all__ = (\[.*\])", s, re.S)
    names = set(eval(m.group(1))) if m else set()
    return names | {"db_pool"}

def check_syntax():
    bad = []
    for m in MODS:
        try: ast.parse(open(f"{m}.py").read())
        except SyntaxError as e: bad.append(f"{m}.py:{e.lineno}: {e.msg}")
    return bad

def check_names():
    CA = core_all(); BI = set(dir(builtins)); issues=[]
    for m in MODS:
        tree = ast.parse(open(f"{m}.py").read())
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

def check_imports():
    os.environ.setdefault("DISCORD_TOKEN","dummy")
    try:
        importlib.import_module("bot"); return []
    except Exception as e:
        return [f"Ошибка импорта: {type(e).__name__}: {e}"]


def check_help_coverage():
    """Мягкая проверка: все ли команды упомянуты в /help (HELP_CATEGORIES)."""
    os.environ.setdefault("DISCORD_TOKEN", "dummy")
    try:
        botmod = importlib.import_module("bot")
        help_text = open("commands_misc.py").read()
        missing = []
        for c in botmod.tree.walk_commands():
            name = c.qualified_name
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
                  ("Импорт всех модулей", check_imports)]:
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
