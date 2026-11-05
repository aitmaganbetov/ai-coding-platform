# Pilot: Laravel + React (test fixture, AC-02)

A small Laravel 11 API with a React front end, used by the S7 acceptance test
`tests/integration/test_pilot_audit.py`. It contains **deliberately planted
problems** so a read-only audit must find them with file paths:

| Where | Problem | Expected detector |
|---|---|---|
| `app/Http/Controllers/ReportController.php` | raw SQL built by concatenation | Semgrep `laravel-raw-sql-concat` |
| `app/Http/Controllers/ReportController.php` | `unserialize()` of a cookie | Semgrep `php-unserialize` |
| `resources/js/components/Comment.jsx` | `dangerouslySetInnerHTML` | Semgrep `react-dangerously-set-inner-html` |
| `composer.lock` | `guzzlehttp/psr7` 1.8.0 (CVE-2022-24775) | Trivy / OSV-Scanner |
| `package-lock.json` | `lodash` 4.17.15 (CVE-2020-8203, CVE-2021-23337) | Trivy / OSV-Scanner |
| `Dockerfile` | container runs as root | Trivy misconfiguration |
| `config/services.php` | AWS key — **written by the test at run time**, never committed | Gitleaks |

It lives under `tests/` so the platform's own self-audit (Semgrep's default ignore)
does not report the planted problems against this repository. Never deploy it.
