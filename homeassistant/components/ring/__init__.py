"""Support for Ring Doorbell/Chimes."""

from __future__ import annotations

from functools import partial
import logging

from ring_doorbell import Auth, Ring

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import APPLICATION_NAME, CONF_TOKEN, __version__
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue

from .const import (
    DOMAIN,
    PLATFORMS,
    RING_API,
    RING_DEVICES,
    RING_DEVICES_COORDINATOR,
    RING_NOTIFICATIONS_COORDINATOR,
)
from .coordinator import RingDataCoordinator, RingNotificationsCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry."""

    def token_updater(token):
        """Handle from sync context when token is updated."""
        hass.loop.call_soon_threadsafe(
            partial(
                hass.config_entries.async_update_entry,
                entry,
                data={**entry.data, CONF_TOKEN: token},
            )
        )

    auth = Auth(
        f"{APPLICATION_NAME}/{__version__}", entry.data[CONF_TOKEN], token_updater
    )
    ring = Ring(auth)

    await _migrate_old_unique_ids(hass, entry.entry_id)

    devices_coordinator = RingDataCoordinator(hass, ring)
    notifications_coordinator = RingNotificationsCoordinator(hass, ring)
    await devices_coordinator.async_config_entry_first_refresh()
    await notifications_coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        RING_API: ring,
        RING_DEVICES: ring.devices(),
        RING_DEVICES_COORDINATOR: devices_coordinator,
        RING_NOTIFICATIONS_COORDINATOR: notifications_coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if hass.services.has_service(DOMAIN, "update"):
        return True

    async def async_refresh_all(_: ServiceCall) -> None:
        """Refresh all ring data."""
        _LOGGER.warning(
            "Detected use of service 'ring.update'. "
            "This is deprecated and will stop working in Home Assistant 2024.10. "
            "Use 'homeassistant.update_entity' instead which updates all ring entities",
        )
        async_create_issue(
            hass,
            DOMAIN,
            "deprecated_service_ring_update",
            breaks_in_ha_version="2024.10.0",
            is_fixable=True,
            is_persistent=False,
            issue_domain=DOMAIN,
            severity=IssueSeverity.WARNING,
            translation_key="deprecated_service_ring_update",
        )

        for info in hass.data[DOMAIN].values():
            await info[RING_DEVICES_COORDINATOR].async_refresh()
            await info[RING_NOTIFICATIONS_COORDINATOR].async_refresh()

    # register service
    hass.services.async_register(DOMAIN, "update", async_refresh_all)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Ring entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    hass.data[DOMAIN].pop(entry.entry_id)

    if len(hass.data[DOMAIN]) != 0:
        return True

    # Last entry unloaded, clean up service
    hass.services.async_remove(DOMAIN, "update")

    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    return True


async def _migrate_old_unique_ids(hass: HomeAssistant, entry_id: str) -> None:
    entity_registry = er.async_get(hass)

    @callback
    def _async_migrator(entity_entry: er.RegistryEntry) -> dict[str, str] | None:
        # Old format for camera and light was int
        if isinstance(entity_entry.unique_id, int):
            new_unique_id = str(entity_entry.unique_id)
            if existing_entity_id := entity_registry.async_get_entity_id(
                entity_entry.domain, entity_entry.platform, new_unique_id
            ):
                _LOGGER.error(
                    "Cannot migrate to unique_id '%s', already exists for '%s', "
                    "You may have to delete unavailable ring entities",
                    new_unique_id,
                    existing_entity_id,
                )
                return None
            _LOGGER.info("Fixing non string unique id %s", entity_entry.unique_id)
            return {"new_unique_id": new_unique_id}
        return None

    await er.async_migrate_entries(hass, entry_id, _async_migrator)
