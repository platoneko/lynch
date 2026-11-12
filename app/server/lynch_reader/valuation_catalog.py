"""Shared interpretation for app, daily report and index."""
from typing import Any, Protocol

from eval.lib.result_view import ResultView as CatalogValuation, map_result


class ValuationProvider(Protocol):
    def current(self, ticker: str) -> dict[str, Any]: ...
