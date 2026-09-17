#!/usr/bin/env python3
"""Bundle the Cloudflare Email Worker into a single deployable module.

Cloudflare's REST API accepts exactly one module per script, so the worker
source in ``src/index.js`` and its one runtime dependency (``postal-mime``,
used to parse MIME) have to be concatenated into a single file.

The bundler is intentionally tiny — it handles the subset of ES module syntax
that ``postal-mime`` actually uses:

* drop ``import`` statements (dependency order is fixed below),
* turn ``export default class X`` into ``class X``,
* drop ``export { … }`` re-export lists,
* turn ``export function f`` into ``function f``.

No npm install is required: the tarball is fetched straight from the registry.

Usage::

    python worker/build.py                # writes dist/atria-otp.js
    python worker/build.py --postal-version 3.0.0
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import tarfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_VERSION = "3.0.0"
REGISTRY = "https://registry.npmjs.org"

#: Concatenation order for postal-mime's source files.  Each entry only uses
#: symbols defined above it, so the order is significant.
POSTAL_MODULES = [
    "decode-strings.js",
    "html-entities.js",
    "base64-encoder.js",
    "base64-decoder.js",
    "pass-through-decoder.js",
    "qp-decoder.js",
    "text-format.js",
    "address-parser.js",
    "mime-node.js",
    "postal-mime.js",
]

_IMPORT_LINE = re.compile(r"^\s*import\s+.*?from\s+['\"].*?['\"];?\s*$", re.M)
_EXPORT_DEFAULT_CLASS = re.compile(r"^export\s+default\s+class\s+", re.M)
_EXPORT_DEFAULT_FUNCTION = re.compile(r"^export\s+default\s+(?:async\s+)?function\s+", re.M)
_EXPORT_DEFAULT_BARE = re.compile(r"^\s*export\s+default\s+[A-Za-z_$][\w$]*\s*;\s*$", re.M)
_EXPORT_BRACES = re.compile(r"^\s*export\s*\{[^}]*\}\s*;?\s*$", re.M)
_EXPORT_KEYWORD = re.compile(r"^export\s+(?=(?:async\s+)?function\s|const\s|let\s|var\s|class\s)", re.M)

#: Statements that must not survive bundling.  The worker's own
#: ``export default { … }`` is the module's required export and is allowed.
_LEFTOVER_IMPORT = re.compile(r"^\s*import\s", re.M)
_LEFTOVER_EXPORT = re.compile(r"^\s*export\s+(?!default\s*\{)", re.M)


def find_leftovers(source: str) -> list:
    """Return offending import/export lines (excluding the worker's own export)."""
    return _LEFTOVER_IMPORT.findall(source) + _LEFTOVER_EXPORT.findall(source)


def strip_module_syntax(source: str) -> str:
    """Inline a single ES module's body."""
    source = _IMPORT_LINE.sub("", source)
    source = _EXPORT_DEFAULT_CLASS.sub("class ", source)
    source = _EXPORT_DEFAULT_FUNCTION.sub(
        lambda match: match.group(0).replace("export default ", ""), source
    )
    source = _EXPORT_DEFAULT_BARE.sub("", source)
    source = _EXPORT_BRACES.sub("", source)
    source = _EXPORT_KEYWORD.sub("", source)
    return source


def fetch_postal_mime(version: str) -> dict:
    """Download and unpack ``postal-mime`` from the npm registry."""
    url = f"{REGISTRY}/postal-mime/-/postal-mime-{version}.tgz"
    print(f"  fetching {url}")
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read()

    modules = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = Path(member.name).name
            if member.isfile() and name in POSTAL_MODULES:
                handle = archive.extractfile(member)
                if handle:
                    modules[name] = handle.read().decode("utf-8")

    missing = [name for name in POSTAL_MODULES if name not in modules]
    if missing:
        raise SystemExit(f"postal-mime {version} is missing: {', '.join(missing)}")
    return modules


def build(postal_version: str, output: Path) -> Path:
    print("bundling atria-otp worker")
    modules = fetch_postal_mime(postal_version)

    chunks = [
        "// ── postal-mime (bundled) ──────────────────────────────────────────────",
        f"// source: npm postal-mime@{postal_version} (MIT)",
        "",
    ]
    for name in POSTAL_MODULES:
        chunks.append(f"// ==== {name} ====")
        chunks.append(strip_module_syntax(modules[name]).strip())
        chunks.append("")

    entry = (HERE / "src" / "index.js").read_text(encoding="utf-8")
    if find_leftovers(entry):
        raise SystemExit(
            "worker/src/index.js must not contain import statements or "
            "named/star exports (only `export default`)"
        )

    chunks.append("// ── worker entry point ────────────────────────────────────────────────")
    chunks.append("")
    chunks.append(entry.strip())
    chunks.append("")

    bundle = "\n".join(chunks)
    leftovers = find_leftovers(bundle)
    if leftovers:
        raise SystemExit(
            f"bundling left {len(leftovers)} import/export statement(s) behind"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(bundle, encoding="utf-8")
    print(f"  wrote {output} ({len(bundle):,} bytes)")
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--postal-version", default=DEFAULT_VERSION)
    parser.add_argument("--output", default=str(HERE / "dist" / "atria-otp.js"))
    args = parser.parse_args(argv)

    build(args.postal_version, Path(args.output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
