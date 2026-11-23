"""Summarise CodeQL SARIF as inspectable machine-readable evidence. No auto-approval.

CodeQL is an independent static analysis engine, not author-disjoint human
review and not a proof that runtime Send-boundary invariants are satisfied.
"""
from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

files = sorted(glob.glob("codeql-results/**/*.sarif", recursive=True))
if not files:
    raise SystemExit("No CodeQL SARIF outputs present: this cannot count as a scan.")

findings = []
tools = []
for filename in files:
    data = json.loads(Path(filename).read_text(encoding="utf-8"))
    for run in data.get("runs", []):
        name = run.get("tool", {}).get("driver", {}).get("name", "unknown")
        version = run.get("tool", {}).get("driver", {}).get("semanticVersion", "")
        tools.append({"engine": name, "version": version, "source": filename})
        for result in run.get("results", []):
            primary = (result.get("locations") or [{}])[0]
            physical = primary.get("physicalLocation", {})
            uri = physical.get("artifactLocation", {}).get("uri", "")
            line = physical.get("region", {}).get("startLine")
            msg = result.get("message", {}).get("text", "")
            findings.append({
                "engine": name,
                "rule_id": result.get("ruleId", ""),
                "level": result.get("level", "warning"),
                "message": msg[:700],
                "path": uri,
                "line": line,
            })

result = {
    "kind": "CODEQL_STATIC_ANALYSIS_SOURCE",
    "source_revision": sys.argv[1] if len(sys.argv) > 1 else "",
    "passed_without_alerts": len(findings) == 0,
    "result_count": len(findings),
    "sarif_files": files,
    "engines": tools,
    "findings": findings,
    "limitations": [
        "Static CodeQL security queries are not exhaustive.",
        "A zero-result scan cannot substitute for independent exact-SHA review.",
        "Private Relay journal, signed-in browser and live effect authority were not scanned.",
        "No real Web or local machine effect was performed.",
    ],
}
text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
Path("cr01-codeql-summary.json").write_text(text, encoding="utf-8")
print(text)
