"""Load and persist the fleet's single source of truth (state.json)."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, fields

from .models import Client, FleetConfig, Server

DEFAULT_STATE_PATH = os.environ.get("AWGFLEET_STATE", "state.json")


def _make(cls, data: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


class State:
    def __init__(self, path: str = DEFAULT_STATE_PATH):
        self.path = path
        self.config: FleetConfig | None = None

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> FleetConfig:
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # tolerate fields written by other awg-fleet versions (e.g. the retired
        # per-client home_host) so an existing state.json keeps loading
        servers = [_make(Server, s) for s in data.pop("servers", [])]
        clients = [_make(Client, c) for c in data.pop("clients", [])]
        known = {f.name for f in fields(FleetConfig)}
        data = {k: v for k, v in data.items() if k in known}
        self.config = FleetConfig(servers=servers, clients=clients, **data)
        return self.config

    def save(self) -> None:
        if self.config is None:
            raise RuntimeError("nothing to save; call load() or set config first")
        if os.path.exists(self.path):
            try:  # keep one rollback copy so a bad edit never loses clients/keys
                shutil.copy2(self.path, self.path + ".bak")
            except OSError:
                pass
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self.config), f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)  # atomic swap so a crash never truncates state
