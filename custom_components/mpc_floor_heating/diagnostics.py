"""Diagnostics download: configuration, model and the last plan."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import MpcConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: MpcConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    return {
        "options": dict(entry.options),
        "active": coordinator.active,
        "settings": asdict(coordinator.settings),
        "model": coordinator.model.as_dict() if coordinator.model else None,
        "model_error": coordinator.model_error,
        "status": data.status if data else None,
        "message": data.message if data else None,
        "room_now": data.room_now if data else None,
        "result": asdict(data.result) if data and data.result else None,
        "outdoor_plan": data.outdoor_plan if data else None,
        "price_plan": data.price_plan if data else None,
    }
