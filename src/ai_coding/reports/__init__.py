"""Reports: JSON, Markdown, SARIF 2.1.0 and JUnit XML (FR-10, P0-08)."""

from ai_coding.reports.audit import AuditReport, render_markdown
from ai_coding.reports.junit import render_junit
from ai_coding.reports.sarif import render_sarif, to_sarif

__all__ = ["AuditReport", "render_junit", "render_markdown", "render_sarif", "to_sarif"]
