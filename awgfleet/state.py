"""Load and persist the fleet's single source of truth (state.json)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict

from .models import Client, FleetConfig, Server

DEFAULT_STATE_PATH = os.environ.get("AWGFLEET_STATE", "state.json")


class State:
    def __init__(self, path: str = DEFAULT_STATE_PATH):
        self.path = path
        self.config: FleetConfig | None = None

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> FleetConfig:
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        servers = [Server(**s) for s in data.pop("servers", [])]
        clients = [Client(**c) for c in data.pop("clients", [])]
        self.config = FleetConfig(servers=servers, clients=clients, **data)
        return self.config

    def save(self) -> None:
        if self.config is None:
            raise RuntimeError("nothing to save; call load() or set config first")
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self.config), f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)  # atomic swap so a crash never truncates state
