"""Paginação acessível e reutilizável para listas da plataforma."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Generic, Sequence, TypeVar


T = TypeVar("T")
PAGE_SIZE = 20


@dataclass(frozen=True)
class PlatformPage(Generic[T]):
    items: tuple[T, ...]
    total: int
    page: int
    per_page: int = PAGE_SIZE

    @property
    def pages(self) -> int:
        return max(1, ceil(self.total / self.per_page))

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def previous_page(self) -> int:
        return max(1, self.page - 1)

    @property
    def next_page(self) -> int:
        return min(self.pages, self.page + 1)

    @property
    def numbers(self) -> tuple[int | None, ...]:
        """Mantém a página atual e evita dezenas de links simultâneos."""
        if self.pages <= 5:
            return tuple(range(1, self.pages + 1))
        candidates = {1, self.pages, self.page - 1, self.page, self.page + 1}
        candidates.update(range(1, min(4, self.pages + 1)))
        candidates.update(range(max(1, self.pages - 1), self.pages + 1))
        ordered = sorted(number for number in candidates if 1 <= number <= self.pages)
        result: list[int | None] = []
        previous = 0
        for number in ordered:
            if previous and number - previous > 1:
                result.append(None)
            result.append(number)
            previous = number
        return tuple(result)


def requested_page(value: object) -> int:
    try:
        page = int(str(value or "1"))
    except (TypeError, ValueError):
        return 1
    return max(1, page)


def paginate_sequence(items: Sequence[T], page: object, *, per_page: int = PAGE_SIZE) -> PlatformPage[T]:
    if per_page <= 0:
        raise ValueError("O tamanho da página deve ser positivo.")
    requested = requested_page(page)
    total = len(items)
    pages = max(1, ceil(total / per_page))
    current = min(requested, pages)
    start = (current - 1) * per_page
    return PlatformPage(tuple(items[start:start + per_page]), total, current, per_page)


__all__ = ["PAGE_SIZE", "PlatformPage", "paginate_sequence", "requested_page"]
