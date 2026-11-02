"""Events every persistent agent link exposes to the conversation service."""
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class LinkEvents:
    on_delta: Callable[[str], None]  # streamed reply text
    on_entry: Callable[[dict], None]  # an AppLog entry just appended
    on_usage: Callable[[int | None, int | None, float | None], None]  # context tokens, window, session cost USD
    on_permission: Callable[[str, str, str, dict], None]  # request id, tool id, normalized name, input
    on_turn_end: Callable[[str | None, str | None], None]  # stop reason, error text
    on_exit: Callable[[bool, str], None]  # expected, stderr tail
