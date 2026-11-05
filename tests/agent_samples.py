"""Valid sample outputs of the CREATE agents (test data shared by unit/integration tests)."""

from __future__ import annotations

import copy
from typing import Any

IDEA = "Сервис коротких ссылок: пользователь сокращает URL и видит статистику переходов."

SPEC: dict[str, Any] = {
    "title": "Link Shortener",
    "summary": "Сервис, который превращает длинные URL в короткие и считает переходы.",
    "goals": ["Сокращать ссылки", "Показывать статистику переходов"],
    "non_goals": ["Пользовательские домены"],
    "constraints": ["Только HTTPS-ссылки"],
    "assumptions": ["Авторизация не нужна в MVP"],
    "functional_requirements": [
        {"id": "FR-01", "text": "Создать короткую ссылку для URL", "priority": "must"},
        {"id": "FR-02", "text": "Перенаправлять по короткой ссылке", "priority": "must"},
        {"id": "FR-03", "text": "Показывать число переходов", "priority": "should"},
    ],
    "non_functional_requirements": [
        {"id": "NFR-01", "category": "performance", "text": "Редирект быстрее 50 мс (p95)"}
    ],
    "user_stories": [
        {"id": "US-01", "as_a": "пользователь", "i_want": "сократить ссылку",
         "so_that": "делиться ей", "requirement_refs": ["FR-01"],
         "acceptance_criteria": ["POST /links возвращает код ссылки"]},
        {"id": "US-02", "as_a": "посетитель", "i_want": "перейти по короткой ссылке",
         "so_that": "попасть на исходный URL", "requirement_refs": ["FR-02", "FR-03"],
         "acceptance_criteria": ["GET /{code} отвечает 301", "Счётчик увеличивается"]},
    ],
    "open_questions": [
        {"id": "Q-01", "question": "Нужен ли срок жизни ссылок?",
         "default_assumption": "Ссылки бессрочные"}
    ],
    "confidence": 0.8,
}  # fmt: skip

ARCH: dict[str, Any] = {
    "stack": {"language": "python", "framework": "FastAPI", "database": "SQLite"},
    "modules": [
        {"name": "links", "responsibility": "Создание и хранение коротких ссылок",
         "depends_on": ["storage"], "requirement_refs": ["FR-01", "FR-02"]},
        {"name": "stats", "responsibility": "Учёт переходов \"по ссылке\"",
         "depends_on": ["storage"], "requirement_refs": ["FR-03"]},
        {"name": "storage", "responsibility": "Доступ к базе данных", "depends_on": [],
         "requirement_refs": []},
    ],
    "adrs": [
        {"id": "ADR-0001", "title": "Модульный монолит на FastAPI",
         "context": "MVP, одна команда", "decision": "Один сервис, три модуля",
         "consequences": "Простое развёртывание", "alternatives": ["Микросервисы"]},
    ],
    "api": [
        {"method": "POST", "path": "/links", "summary": "Создать ссылку", "module": "links"},
        {"method": "GET", "path": "/{code}", "summary": "Редирект", "module": "links"},
    ],
    "data_model": [
        {"entity": "Link", "description": "Короткая ссылка",
         "fields": [{"name": "code", "type": "str", "required": True},
                    {"name": "url", "type": "str", "required": True}]},
    ],
    "confidence": 0.7,
}  # fmt: skip

PLAN: dict[str, Any] = {
    "items": [
        {"id": "T-001", "title": "Схема БД", "description": "Таблица links", "type": "infra",
         "module": "storage", "requirement_refs": [], "depends_on": [], "estimate_points": 2,
         "priority": "P0", "acceptance": ["Миграция применяется"]},
        {"id": "T-002", "title": "Создание ссылки", "description": "POST /links",
         "type": "feature", "module": "links", "requirement_refs": ["FR-01"],
         "depends_on": ["T-001"], "estimate_points": 3, "priority": "P0",
         "acceptance": ["Тест на создание проходит"]},
        {"id": "T-003", "title": "Редирект и счётчик", "description": "GET /{code}",
         "type": "feature", "module": "links", "requirement_refs": ["FR-02", "FR-03"],
         "depends_on": ["T-002"], "estimate_points": 5, "priority": "P0",
         "acceptance": ["301 и счётчик"]},
    ],
    "milestones": [
        {"name": "MVP", "goal": "Рабочие ссылки", "items": ["T-001", "T-002", "T-003"]}
    ],
    "confidence": 0.75,
}  # fmt: skip


def spec(**changes: Any) -> dict[str, Any]:
    """Deep copy of :data:`SPEC` with top-level changes."""
    return {**copy.deepcopy(SPEC), **changes}


def arch(**changes: Any) -> dict[str, Any]:
    """Deep copy of :data:`ARCH` with top-level changes."""
    return {**copy.deepcopy(ARCH), **changes}


def plan(**changes: Any) -> dict[str, Any]:
    """Deep copy of :data:`PLAN` with top-level changes."""
    return {**copy.deepcopy(PLAN), **changes}
