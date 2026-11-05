"""JUnit XML export: one test case per Quality Gate and per tool (FR-10)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

from ai_coding.security import redact


def render_junit(report: dict[str, Any]) -> str:
    """JUnit XML so CI systems display gate results natively."""
    root = ET.Element("testsuites", name=f"ai-coding audit {report.get('project', '')}")
    gates = ET.SubElement(root, "testsuite", name="quality-gates")
    failures = skipped = 0
    for gate in report.get("gates", []):
        case = ET.SubElement(gates, "testcase", classname="quality-gates", name=gate["gate"])
        reason = redact(str(gate.get("reason", "")))
        if gate["result"] == "FAIL":
            failures += 1
            failure = ET.SubElement(
                case,
                "failure",
                message=reason[:1000],
                type="blocking" if gate.get("blocking") else "advisory",
            )
            failure.text = redact("\n".join(gate.get("evidence", [])))
        elif gate["result"] == "SKIPPED":
            skipped += 1
            ET.SubElement(case, "skipped", message=reason[:1000])
    gates.set("tests", str(len(report.get("gates", []))))
    gates.set("failures", str(failures))
    gates.set("skipped", str(skipped))
    gates.set("errors", "0")

    tools = ET.SubElement(root, "testsuite", name="tools")
    errors = 0
    for tool in report.get("tools", []):
        case = ET.SubElement(
            tools,
            "testcase",
            classname="tools",
            name=tool["tool"],
            time=str(tool.get("duration_seconds", 0)),
        )
        if not tool["succeeded"]:
            errors += 1
            error = ET.SubElement(case, "error", message=redact(str(tool.get("error")))[:1000])
            error.text = redact(str(tool.get("error")))
    tools.set("tests", str(len(report.get("tools", []))))
    tools.set("failures", "0")
    tools.set("errors", str(errors))
    tools.set("skipped", "0")
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
