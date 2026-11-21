from dataclasses import dataclass
from datetime import datetime


@dataclass
class FrozenClock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant
