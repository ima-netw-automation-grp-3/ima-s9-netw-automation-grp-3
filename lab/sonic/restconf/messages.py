"""Messages d'erreur de libyang et de GCU rendus lisibles. Module pur."""

from __future__ import annotations

import re

_DATA_PATH = re.compile(r"\.?:? Data path: (\S+?)(?: \(line \d+\))?\.?$")
_LINE = re.compile(r" \(line \d+\)\.?$")
_PREFIXES = ("failed to parse data tree: ", "Data Loading Failed\n", "Data Loading Failed:")


def libyang_message(text: str) -> tuple[str, str | None]:
    """« failed to parse data tree: X.: Data path: /a/b (line 1) » → ("X", "/a/b")."""
    message = str(text).strip()
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if message.startswith(prefix):
                message = message[len(prefix):].strip()
                changed = True
    path = None
    first, _, rest = message.partition("\n")
    m = _DATA_PATH.search(first)
    if m:
        path = m.group(1)
        first = first[: m.start()].rstrip(" .:")
    first = _LINE.sub("", first)
    message = "\n".join(p for p in (first, rest.strip()) if p)
    return message, path


def gcu_message(output: str) -> str:
    """Erreur de « config apply-patch » : la ligne « Error: … » et sa suite, sans répétitions."""
    lines = output.splitlines()
    picked: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("Error: "):
            picked = [line[len("Error: "):]]
            for nxt in lines[i + 1:]:
                if not nxt.strip() or nxt.startswith("sonic_yang("):
                    break
                picked.append(nxt)
            break
    if not picked:
        for line in lines:
            if line.startswith("Failed to apply patch due to: "):
                picked = [line[len("Failed to apply patch due to: "):]]
                break
    text = "\n".join(picked).strip() or output.strip()[-1500:]
    # « Validate json patch: [ …le patch… ] failed due to: X » : le patch est déjà connu du client
    text = re.sub(r"^Validate json patch: \[.*?\] failed due to: ", "", text, flags=re.S)
    message, path = libyang_message(text)
    return f"{message} ({path})" if path else message
