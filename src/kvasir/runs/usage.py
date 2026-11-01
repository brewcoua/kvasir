"""What a model, a role or a stage spent, added up call by call."""

from __future__ import annotations

from dataclasses import dataclass

from kvasir.schemas.runs import ModelUsage, StageUsage


@dataclass
class Usage:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0
    embedding_tokens: int = 0
    searches: int = 0
    seconds: float = 0.0

    def add(
        self, prompt_tokens: int, completion_tokens: int, reasoning_tokens: int, cost: float
    ) -> None:
        self.calls += 1
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.reasoning_tokens += reasoning_tokens
        self.cost += cost

    def model(self) -> ModelUsage:
        return ModelUsage(
            calls=self.calls,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            reasoning_tokens=self.reasoning_tokens,
            cost=round(self.cost, 6),
            seconds=round(self.seconds, 3),
        )

    def stage(self) -> StageUsage:
        return StageUsage(
            **self.model().model_dump(),
            embedding_tokens=self.embedding_tokens,
            searches=self.searches,
        )
