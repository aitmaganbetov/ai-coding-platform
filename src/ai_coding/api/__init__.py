"""Self-Hosted server: REST API, RBAC, job queue and workers (ТЗ §13, §15; ADR-0014, ADR-0030).

Install with the ``server`` extra (``pip install 'ai-coding-platform[server]'``);
PostgreSQL needs the ``postgres`` extra. The CLI does not import this package unless
an ``ai-coding server`` command runs.
"""
