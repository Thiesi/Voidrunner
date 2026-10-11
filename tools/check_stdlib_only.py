"""Fail if voidrunner.py imports anything outside Python's standard library.

The door ships as one file that a SysOp points a BBS at; a third-party import
would make it depend on whatever happens to be installed on that host.

    python tools/check_stdlib_only.py
"""

import ast
import pathlib
import sys

source = pathlib.Path(__file__).resolve().parent.parent / "voidrunner.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
modules = set()
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        modules.update(alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
        modules.add(node.module.split(".")[0])
outside = sorted(m for m in modules if m not in sys.stdlib_module_names)
if outside:
    sys.exit(f"voidrunner.py imports non-stdlib modules: {', '.join(outside)}")
print(f"voidrunner.py imports only the standard library ({len(modules)} modules)")
