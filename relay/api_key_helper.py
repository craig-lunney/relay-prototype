"""
Relay API key helper.

Invoked via Claude's apiKeyHelper mechanism.  Reads the API key from the file
path supplied as the sole command-line argument and writes it to stdout.

Design constraints:
- Accepts exactly one argument: the path to the key file.
- Reads only that file.
- Writes only the key (stripped of surrounding whitespace) to stdout.
- Emits no logging, diagnostics, or helpful error messages on failure —
  exit(1) is the only failure mode, giving potential observers nothing to act on.
- No environment variables are read.
- No stdin is read.
- No workspace or repository content is accessed.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(1)
    try:
        content = Path(sys.argv[1]).resolve().read_text(encoding="ascii", errors="strict").strip()
    except Exception:
        sys.exit(1)
    sys.stdout.write(content)


if __name__ == "__main__":
    main()
