# Pilot projects (AC-09)

Two small projects on different stacks that exercise the platform's reusable GitHub
Actions workflows inside this repository (`.github/workflows/pilots.yml`):

| Pilot | Stack | Workflows |
| --- | --- | --- |
| [`pilot-python`](pilot-python) | Python (uv, pytest) | `test-python.yml`, `quality.yml` (ruff in the sandbox) |
| [`pilot-node`](pilot-node) | JavaScript (Node 22, `node --test`) | `test-node.yml`, `security.yml` |

They are intentionally tiny: their job is to prove that a project on each stack gets
its check statuses in a pull request.
