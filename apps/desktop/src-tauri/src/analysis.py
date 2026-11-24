"""Read-only data analysis that runs without the permission prompt (apps/desktop/src-tauri/src/approval.rs).

`--check FILE`: exit 0 when the script only does read-only analysis (else exit 3 and say why, nothing runs).
`FILE`: run it with file access confined: open() reads only, and only files under a `lumio_data` folder in the home
folder (where Lumio saves tables for scripts); glob only lists files there.

What counts as read-only analysis (checked on the script's syntax tree, before anything runs):
- imports only plain data modules (no os, sys, subprocess, socket, pathlib, shutil, pandas...), and reaches no
  dangerous module through another (glob.os, json.codecs...);
- no exec / eval / compile / getattr / setattr / globals / vars / __import__ / breakpoint / input;
- no name or attribute that starts with "_", so nothing can reach around the confinement.
Anything else gets the usual prompt.
"""

import ast
import builtins
import glob as glob_module
import os
import sys

# Modules that only compute. Left out on purpose: operator / string (reach attributes from text), typing (evaluates
# annotations), dataclasses / enum (generate code), and anything with files, processes or the network.
ALLOWED = {
    "json", "glob", "csv", "collections", "decimal", "math", "statistics", "datetime", "re", "itertools", "functools",
    "heapq", "bisect", "textwrap", "fractions", "pprint", "calendar", "time", "zoneinfo", "copy",
}
# Allowed modules hold dangerous ones as attributes (glob.os, json.codecs, datetime.sys): no attribute by these names.
BLOCKED_ATTRS = {
    "os", "sys", "subprocess", "builtins", "importlib", "io", "shutil", "socket", "posix", "nt", "pathlib", "ctypes",
    "signal", "threading", "multiprocessing", "pickle", "marshal", "codecs", "locale", "copyreg", "warnings", "weakref",
    "system", "popen", "spawn", "environ", "getenv", "putenv", "fork", "kill", "remove", "unlink", "rmdir", "removedirs",
    "rename", "renames", "replace_file", "chmod", "chown", "link", "symlink", "truncate", "mkdir", "makedirs", "scandir",
    "listdir", "walk", "exec", "eval", "load_module", "modules", "loader", "settrace", "setprofile", "gc", "open",
}
BANNED = {"exec", "eval", "compile", "getattr", "setattr", "delattr", "globals", "locals", "vars", "breakpoint", "input",
          "help", "memoryview", "classmethod", "staticmethod", "property", "super", "object", "type"}


def problem(source):
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"it doesn't parse ({exc.msg})"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED:
                    return f"it imports {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] not in ALLOWED:
                return f"it imports from {node.module or '.'}"
        elif isinstance(node, ast.Name) and (node.id.startswith("_") or node.id in BANNED):
            return f"it uses {node.id}"
        elif isinstance(node, ast.Attribute) and (node.attr.startswith("_") or node.attr in BLOCKED_ATTRS):
            return f"it uses .{node.attr}"
        elif isinstance(node, ast.ClassDef):
            return "it defines a class"
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("_"):
            return f"it defines {node.name}"
        elif isinstance(node, ast.arg) and node.arg.startswith("_"):
            return f"it takes {node.arg}"
        elif isinstance(node, ast.keyword) and (node.arg or "").startswith("_"):
            return f"it passes {node.arg}"
        elif isinstance(node, ast.alias) and (node.asname or "").startswith("_"):
            return f"it renames to {node.asname}"
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            return "it changes global names"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
            mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == "mode"), None)
            if mode is not None and not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)
                                         and not any(flag in mode.value for flag in "wax+")):
                return "it opens a file to write"
    return ""


def data_root_ok(path):
    """A real path inside a `lumio_data` folder under the home folder."""
    real = os.path.realpath(os.fspath(path))
    home = os.path.realpath(os.path.expanduser("~"))
    parts = real.split(os.sep)
    return real.startswith(home + os.sep) and "lumio_data" in parts


def confine():
    real_open = builtins.open
    real_glob, real_iglob = glob_module.glob, glob_module.iglob

    def safe_open(file, mode="r", *args, **kwargs):
        if any(flag in str(mode) for flag in "wax+"):
            raise PermissionError("read-only analysis: files can't be written")
        if not data_root_ok(file):
            raise PermissionError("read-only analysis: only files under lumio_data can be read")
        return real_open(file, mode, *args, **kwargs)

    def safe_glob(pattern, *args, **kwargs):
        return [p for p in real_glob(pattern, *args, **kwargs) if data_root_ok(p)]

    def safe_iglob(pattern, *args, **kwargs):
        return (p for p in real_iglob(pattern, *args, **kwargs) if data_root_ok(p))

    builtins.open = safe_open
    glob_module.glob, glob_module.iglob = safe_glob, safe_iglob


def main(argv):
    check = argv[:1] == ["--check"]
    path = argv[1] if check else argv[0]
    with open(path, encoding="utf-8") as handle:
        source = handle.read()
    why = problem(source)
    if why:
        print(f"not read-only analysis: {why}")
        return 3
    if check:
        return 0
    confine()
    code = compile(source, "analysis.py", "exec")
    exec(code, {"__name__": "__main__", "__builtins__": builtins})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
