"""The model can select documented mappings; it cannot supply executable code."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CleaningPlan(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)

    version: Literal[1] = 1
    column_mapping: dict[str, str]
    drop_exact_duplicates: bool = True
    null_policy: Literal['preserve'] = 'preserve'
    invalid_policy: Literal['quarantine'] = 'quarantine'
    notes: str = Field(default='', max_length=2000)

    @field_validator('column_mapping')
    @classmethod
    def unique_targets(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value.values()) != len(set(value.values())):
            raise ValueError('Multiple columns cannot map to the same target')
        return value
