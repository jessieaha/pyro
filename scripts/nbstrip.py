#!/usr/bin/env python3
"""Git clean filter: notebook JSON on stdin -> same notebook without outputs on stdout.

Registered for notebooks/*.ipynb in .gitattributes; enable once per clone with
    git config filter.nbstrip.clean "python3 scripts/nbstrip.py"
Outputs stay in the working copy; only the staged/committed version is stripped,
so event-keyed results never reach the public repository.
"""

import json
import sys


def strip(nb: dict) -> dict:
    for cell in nb.get("cells", []):
        if cell.get("cell_type") == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
        cell.get("metadata", {}).pop("execution", None)
    nb.get("metadata", {}).pop("widgets", None)
    return nb


if __name__ == "__main__":
    nb = json.load(sys.stdin)
    json.dump(strip(nb), sys.stdout, indent=1, ensure_ascii=False)
    sys.stdout.write("\n")
