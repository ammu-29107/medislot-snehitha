# MediSlot — Proposed Repository Structure

## 1. Purpose

This document defines the planned repository organisation for MediSlot. It is a design-time structure intended to keep infrastructure, application code, database assets, tests, and documentation discoverable.

```text
medislot-snehitha/
├── .github/
│   └── workflows/
│       └── deploy.yaml
├── cloudformation/
│   ├── network.yaml
│   ├── data.yaml
│   ├── auth.yaml
│   ├── doctor.yaml
│   ├── patient.yaml
│   ├── appointment.yaml
│   ├── waitlist.yaml
│   ├── health-camp.yaml
│   ├── notification.yaml
│   ├── monitoring.yaml
│   └── dashboard.yaml
├── src/
│   ├── auth/
│   │   ├── register/
│   │   ├── login/
│   │   ├── refresh/
│   │   ├── logout/
│   │   └── authorizer/
│   ├── doctor/
│   ├── patient/
│   ├── appointment/
│   ├── availability/
│   ├── waitlist/
│   ├── health_camp/
│   ├── notification/
│   └── shared/
├── database/
│   ├── schema.sql
│   ├── migrations/
│   └── seeds/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   └── fixtures/
├── docs/
│   ├── architecture/
│   ├── design/
│   ├── database/
│   ├── deployment/
│   └── testing/
├── config.json
├── requirements.txt
├── README.md
└── .gitignore
```

## 2. Directory responsibilities

- `.github/workflows/`: CI/CD workflow definitions.
- `cloudformation/`: infrastructure-as-code templates grouped by domain/foundation.
- `src/`: Python Lambda handlers and shared application modules.
- `database/`: logical schema, versioned migrations, and non-sensitive seed data.
- `tests/`: unit, integration, contract, and fixture assets.
- `docs/`: architecture, design decisions, database, deployment, and testing documentation.
- `config.json`: non-sensitive environment/project configuration.
- `requirements.txt`: Python dependencies.
- `README.md`: project overview, prerequisites, architecture summary, and development/deployment instructions.
- `.gitignore`: excludes local environments, caches, credentials, generated artefacts, and editor/OS files.

## 3. README skeleton

The repository README is planned to contain:
1. Project overview and problem statement.
2. Main user roles and capabilities.
3. High-level architecture.
4. Technology stack.
5. Repository structure.
6. Prerequisites and local setup.
7. Configuration and secret-handling guidance.
8. Test instructions.
9. Deployment overview.
10. Links to detailed design documentation.

## 4. `.gitignore` baseline

The ignore rules should cover:
- Python virtual environments (`.venv/`, `venv/`).
- Python cache/bytecode (`__pycache__/`, `*.py[cod]`).
- Test and tooling caches (`.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`).
- Environment files containing secrets (`.env`, `.env.*`, with an explicit exception only for a safe example template).
- Local AWS credentials/config overrides.
- Build/package outputs and temporary deployment artefacts.
- IDE/editor and operating-system files.

Do not ignore source templates, CloudFormation templates, tests, schema files, or non-sensitive example configuration required for the project.
