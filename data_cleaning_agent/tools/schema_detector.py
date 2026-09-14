"""Deterministic aliases. Ambiguous mappings fail before cleaning."""

import re
from dataclasses import asdict, dataclass

from ..agent.schemas import COLUMN_ALIASES, DERIVED_INPUT_FIELDS, REQUIRED_FIELDS
from ..models.cleaning_plan import CleaningPlan


def normalize_name(name: str) -> str:
    return re.sub(r'[\s_-]+', '', name.strip().lower())


@dataclass
class SchemaReport:
    column_mapping: dict[str, str]
    missing_required: list[str]
    unknown_columns: list[str]
    ignored_derived_columns: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def detect_schema(columns) -> SchemaReport:
    names = list(columns)
    if not all(isinstance(name, str) for name in names):
        raise ValueError('Column names must be strings')
    if len(names) != len(set(names)):
        raise ValueError('Duplicate input column names')
    lookup = {normalize_name(alias): canonical for canonical, aliases in COLUMN_ALIASES.items() for alias in aliases}
    mapping, unknown, derived = {}, [], []
    normalized_derived = {normalize_name(name) for name in DERIVED_INPUT_FIELDS}
    for name in names:
        normalized = normalize_name(name)
        canonical = lookup.get(normalized)
        if canonical:
            if canonical in mapping.values():
                raise ValueError(f'Ambiguous aliases for {canonical}; supply one source column')
            mapping[name] = canonical
        elif normalized in normalized_derived:
            derived.append(name)
        else:
            unknown.append(name)
    return SchemaReport(mapping, sorted(REQUIRED_FIELDS - set(mapping.values())), unknown, derived)


def validate_plan(plan: CleaningPlan, schema: SchemaReport) -> None:
    if schema.missing_required:
        raise ValueError('Missing required fields: ' + ', '.join(schema.missing_required))
    if plan.column_mapping != schema.column_mapping:
        raise ValueError('Plan must use exactly the recognized source column mappings')
