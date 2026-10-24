"""Platform for switch integration."""
from __future__ import annotations

from homeassistant.components.climate import ATTR_HVAC_MODES, HVACMode
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONF_POWER_SWITCH,
    CONF_OSCILLATION,
    CONF_SIREN,
    CONF_KEEP_WARM,
    CONF_CHILD_LOCK,
    CONF_DISPLAY_SWITCH,
    CONF_CLIMATE_ENTITY,
    CONF_VERTICAL_DIRECTION,
)
from .entity import HomeKitDeviceEntity, HomeKitDeviceSwitch

# Modes the dry switch must not hand back to when it turns off: off would read
# as switching the unit off, and fan-only is reached through the fan instead.
NOT_RESTORABLE_MODES = (HVACMode.OFF, HVACMode.DRY, HVACMode.FAN_ONLY)


# The louver select's option that swings the vanes, and the fixed position to
# fall back to when swing is turned off before any other position was seen.
SWING_OPTION = "swing"
DEFAULT_VERTICAL_POSITION = "position_3"


def dry_mode_unique_id(entry_id: str, climate_entity: str) -> str:
    """Unique id of an air conditioner's dry switch, shared with climate.py."""
    return f"{DOMAIN}_{entry_id}_{climate_entity}_dry_mode"


def swing_unique_id(entry_id: str, vertical_select: str) -> str:
    """Unique id of an air conditioner's swing switch, shared with climate.py."""
    return f"{DOMAIN}_{entry_id}_{vertical_select}_swing"


class HomeKitSwingSwitch(HomeKitDeviceEntity, SwitchEntity):
    """Vertical swing as a switch for the air conditioner's tile.

    The bridge puts swing on the air conditioner service, where the Home app
    shows it nowhere, and HomeKit has no control for fixed louver positions.
    Categorised so the bridge never gives it its own tile.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:arrow-oscillating"

    def __init__(self, hass: HomeAssistant, entry_id: str, vertical_select: str) -> None:
        """Initialize the swing switch."""
        super().__init__(hass, entry_id, "Swing", vertical_select)
        self._attr_unique_id = swing_unique_id(entry_id, vertical_select)
        self._last_position = DEFAULT_VERTICAL_POSITION

    async def async_update_from_source(self, state) -> None:
        """Read swing from the louver select, remembering the last fixed position."""
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._attr_available = True
        self._attr_is_on = state.state == SWING_OPTION
        if state.state != SWING_OPTION:
            self._last_position = state.state
        self.async_write_ha_state()

    async def _select(self, option: str) -> None:
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": self._source_entity, "option": option},
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Swing the vanes."""
        await self._select(SWING_OPTION)

    async def async_turn_off(self, **kwargs) -> None:
        """Hold the vanes at the last fixed position."""
        await self._select(self._last_position)


class HomeKitDryModeSwitch(HomeKitDeviceEntity, SwitchEntity):
    """Dry mode as a switch for the air conditioner's tile.

    The bridge hides dry whenever cool exists, so HomeKit has no way to pick
    it. The switch is categorised to keep it from becoming its own tile; the
    air conditioner lists it for the patched bridge to add inside its tile.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:water-percent"

    def __init__(self, hass: HomeAssistant, entry_id: str, climate_entity: str) -> None:
        """Initialize the dry mode switch."""
        super().__init__(hass, entry_id, "Dry Mode", climate_entity)
        self._attr_unique_id = dry_mode_unique_id(entry_id, climate_entity)
        self._restore_mode = HVACMode.COOL

    async def async_update_from_source(self, state) -> None:
        """Read dry from the climate's mode, remembering the mode to go back to."""
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._attr_available = True
        self._attr_is_on = state.state == HVACMode.DRY
        if state.state not in NOT_RESTORABLE_MODES:
            self._restore_mode = state.state
        self.async_write_ha_state()

    async def _set_hvac_mode(self, hvac_mode: str) -> None:
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": self._source_entity, "hvac_mode": hvac_mode},
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Switch the unit to dry."""
        await self._set_hvac_mode(HVACMode.DRY)

    async def async_turn_off(self, **kwargs) -> None:
        """Leave dry for the mode the unit was in before it."""
        # Only from dry: turning off a switch that already reads off must not
        # change whatever mode the unit is in now.
        if self.is_on:
            await self._set_hvac_mode(self._restore_mode)


def _has_dry_mode(hass: HomeAssistant, climate_entity: str) -> bool:
    """Whether the climate offers dry, assuming it does until its state loads.

    Integrations like hOn come up after this one; skipping the switch on a
    missing state would lose it for good after a restart.
    """
    state = hass.states.get(climate_entity)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return True
    return HVACMode.DRY in (state.attributes.get(ATTR_HVAC_MODES) or [])

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the HomeKit Device switches."""
    device_type = hass.data[DOMAIN][config_entry.entry_id]["device_type"]
    entities = []

    # Common power entity — only wrap if it's a switch domain entity. Fan
    # domain power entities are wrapped by fan.py instead. Electric blankets
    # manage power implicitly (the climate zones drive the master power) and
    # kettles carry power as their thermostat's Off/Heat mode, so neither
    # exposes a standalone power switch.
    power = config_entry.data.get(CONF_POWER_SWITCH)
    if power and power.startswith("switch.") and device_type not in ("electric_blanket", "kettle"):
        entities.append(
            HomeKitDeviceSwitch(
                hass,
                config_entry.entry_id,
                "Power",
                power,
            )
        )

    # Device-specific switches
    if device_type == "kettle":
        if keep_warm := config_entry.data.get(CONF_KEEP_WARM):
            entities.append(
                HomeKitDeviceSwitch(
                    hass,
                    config_entry.entry_id,
                    "Keep Warm",
                    keep_warm,
                )
            )

    elif device_type == "fan":
        if oscillation := config_entry.data.get(CONF_OSCILLATION):
            entities.append(
                HomeKitDeviceSwitch(
                    hass,
                    config_entry.entry_id,
                    "Oscillation",
                    oscillation,
                )
            )

    elif device_type == "security_system":
        if siren := config_entry.data.get(CONF_SIREN):
            entities.append(
                HomeKitDeviceSwitch(
                    hass,
                    config_entry.entry_id,
                    "Siren",
                    siren,
                )
            )

    elif device_type == "air_purifier":
        if child_lock := config_entry.data.get(CONF_CHILD_LOCK):
            entities.append(
                HomeKitDeviceSwitch(
                    hass,
                    config_entry.entry_id,
                    "Child Lock",
                    child_lock,
                )
            )
        if display_switch := config_entry.data.get(CONF_DISPLAY_SWITCH):
            entities.append(
                HomeKitDeviceSwitch(
                    hass,
                    config_entry.entry_id,
                    "Display",
                    display_switch,
                )
            )

    elif device_type == "air_conditioner":
        climate_entity = config_entry.data.get(CONF_CLIMATE_ENTITY)
        if vertical_select := config_entry.data.get(CONF_VERTICAL_DIRECTION):
            entities.append(
                HomeKitSwingSwitch(hass, config_entry.entry_id, vertical_select)
            )
        if climate_entity and _has_dry_mode(hass, climate_entity):
            entities.append(
                HomeKitDryModeSwitch(hass, config_entry.entry_id, climate_entity)
            )

    if entities:
        async_add_entities(entities)
