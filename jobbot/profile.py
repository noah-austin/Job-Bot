"""The candidate profile: the single source of truth for anything the bot says about you."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Personal(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    website: str = ""


class Companies(BaseModel):
    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)


class Search(BaseModel):
    titles: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    min_salary: int = 0
    seniority: list[str] = Field(default_factory=list)
    companies: Companies = Field(default_factory=Companies)


class Profile(BaseModel):
    personal: Personal
    resume_path: str = ""
    resume_text: str = ""
    search: Search = Field(default_factory=Search)
    answers: dict[str, str] = Field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.personal.first_name} {self.personal.last_name}"

    def facts_for_llm(self) -> str:
        """Everything the model is allowed to use when filling in an application."""
        return yaml.safe_dump(
            {
                "personal": self.personal.model_dump(),
                "approved_answers": self.answers,
                "resume": self.resume_text,
            },
            sort_keys=True,
            allow_unicode=True,
        )


def load_profile(path: Path) -> Profile:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Copy profile.example.yaml to {path} and fill it in.")
    return Profile.model_validate(yaml.safe_load(path.read_text()))
