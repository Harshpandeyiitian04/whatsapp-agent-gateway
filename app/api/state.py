from dataclasses import dataclass

from app.clock import Clock
from app.config import Settings
from app.db import Pool


@dataclass
class AppState:
    settings: Settings
    pool: Pool
    clock: Clock
