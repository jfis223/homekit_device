"""Platform for climate integration."""
from __future__ import annotations

import asyncio

from homeassistant.components.climate import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_FAN_MODE,
    ATTR_FAN_MODES,
    ATTR_HUMIDITY,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODES,
    ATTR_MAX_TEMP,
    ATTR_MIN_TEMP,
    ATTR_SWING_MODE,
    ATTR_SWING_MODES,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ATTR_TARGET_TEMP_STEP,
    FAN_OFF,
    SWING_OFF,
    SWING_ON,
    SWING_VERTICAL,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    STATE_ON,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event

from .const import (
    DOMAIN,
    CONF_CURRENT_TEMP,
    CONF_POWER_SWITCH,
    CONF_TARGET_TEMP,
    CONF_ZONE_BODY,
    CONF_ZONE_FEET,
    CONF_CLIMATE_ENTITY,
    CONF_PROGRAM_SELECT,
    CONF_FAN_ONLY_OPTION,
    CONF_VERTICAL_DIRECTION,
    CONF_LINKED_SWITCHES,
    DEFAULT_FAN_ONLY_OPTION,
    ATTR_HOMEKIT_LINKED_SWITCHES,
)
from .entity import HomeKitDeviceEntity
from .switch import dry_mode_unique_id

# localtuya presents friendly options; index 0 ("Off") maps to the raw level_1.
DEFAULT_OPTIONS = ["Off", "1", "2", "3", "4", "5", "6"]
OFF_OPTION = "Off"
UNAVAILABLE_STATES = (None, "unknown", "unavailable")
# Coalesce rapid HomeKit slider changes: only the latest value is pushed to the
# device after this quiet period, so a quick drag doesn't queue a run of writes.
DEBOUNCE_SECONDS = 0.5

class HomeKitDeviceClimate(HomeKitDeviceEntity, ClimateEntity):
    """Climate proxy mapping a heat-level select onto a HomeKit heater.

    A zone select like ['Off','1'..'6'] is presented as Off/Heat with a
    0-6 target temperature slider, which the HomeKit bridge exposes as a
    Heater/HeaterCooler service.
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_target_temperature_step = 1
    # Min must be >= 1: HA's HomeKit bridge treats a min of 0 as "unset" and
    # substitutes its own default (~7°C), which collides with max and produces a
    # broken range. Heat levels are 1-6; "off" is the on/off state, not level 0.
    _attr_min_temp = 1
    _attr_max_temp = 6
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    # On a cold power-on the blanket briefly resets its zones to Off; wait this
    # long before sending the level so that reset can't clobber it. Only applies
    # when powering on from cold (skipped if already on). Commands are
    # serialized by _cmd_lock, so this wait never causes a race.
    _power_on_settle = 1.5

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        name: str,
        entity_id: str,
        power_entity: str | None = None,
        all_zones: list[str] | None = None,
    ) -> None:
        """Initialize the climate proxy."""
        super().__init__(hass, entry_id, name, entity_id)
        self._power_entity = power_entity
        self._all_zones = all_zones or []
        # Serialize commands so a quick on-then-change can't race (the later
        # command always wins instead of a delayed earlier one clobbering it).
        self._cmd_lock = asyncio.Lock()
        self._options = DEFAULT_OPTIONS
        self._last_heat_level = "1"
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_target_temperature = 1
        self._attr_current_temperature = 1
        self._desired_level = 1
        self._apply_cancel = None

    async def async_added_to_hass(self) -> None:
        """Size the level slider from the source select's option list."""
        if (state := self.hass.states.get(self._source_entity)) is not None:
            if options := state.attributes.get("options"):
                self._options = list(options)
                # Heat levels are the options excluding "Off"; map to a 0..N
                # slider so blankets with fewer/more levels adapt automatically.
                self._attr_max_temp = max(1, len(self._options) - 1)
        await super().async_added_to_hass()

    async def async_set_temperature(self, **kwargs) -> None:
        """Set the heat level from the HomeKit temperature slider."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        self._queue_desired(int(temp))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Map Off/Heat from HomeKit onto the source select."""
        self._queue_desired(0 if hvac_mode == HVACMode.OFF else int(self._last_heat_level))

    def _queue_desired(self, level: int) -> None:
        """Record the latest intent, reflect it immediately, debounce the write.

        HomeKit can fire several rapid changes during a slider drag. We show the
        user's value right away so the slider doesn't snap back to a lagging
        device value, and only push the final value to the device after a short
        quiet period.
        """
        self._desired_level = level
        if level <= 0:
            self._attr_hvac_mode = HVACMode.OFF
        else:
            self._attr_hvac_mode = HVACMode.HEAT
            self._attr_target_temperature = level
            self._last_heat_level = str(level)
        self.async_write_ha_state()
        if self._apply_cancel is not None:
            self._apply_cancel()
        self._apply_cancel = async_call_later(
            self.hass, DEBOUNCE_SECONDS, self._apply_desired
        )

    async def _apply_desired(self, _now) -> None:
        """Push the latest desired level to the device after the debounce."""
        self._apply_cancel = None
        level = self._desired_level
        async with self._cmd_lock:
            if level <= 0:
                await self._select_option(OFF_OPTION)
                await self._maybe_power_off()
            else:
                # Blanket only accepts a heat level while powered on, so turn
                # master power on first, then set the zone level.
                await self._power_on()
                await self._select_option(str(level))

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending debounced write on removal."""
        if self._apply_cancel is not None:
            self._apply_cancel()
            self._apply_cancel = None
        await super().async_will_remove_from_hass()

    async def _maybe_power_off(self) -> None:
        """Turn master power off once every zone on the blanket is Off.

        Only called from the user's own Off command (never from device state
        updates), so it can't be triggered by transient device chatter.
        """
        if not self._power_entity:
            return
        for zone in self._all_zones:
            if zone == self._source_entity:
                continue
            state = self.hass.states.get(zone)
            if state is not None and state.state not in (OFF_OPTION, *UNAVAILABLE_STATES):
                return  # another zone is still heating; leave power on
        await self.hass.services.async_call(
            "switch", "turn_off",
            {"entity_id": self._power_entity}, blocking=True,
        )

    async def _power_on(self) -> None:
        """Ensure master power is on before setting a level.

        Skips entirely if power is already on (so mid-use adjustments are
        instant). On a cold start it powers on then waits `_power_on_settle`
        for the device's power-on zone reset to finish before the level is sent.
        """
        if not self._power_entity:
            return
        state = self.hass.states.get(self._power_entity)
        if state is not None and state.state == "on":
            return
        await self.hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": self._power_entity}, blocking=True,
        )
        await asyncio.sleep(self._power_on_settle)

    async def _select_option(self, option: str) -> None:
        """Forward a friendly option to the source select."""
        await self.hass.services.async_call(
            "select", "select_option",
            {"entity_id": self._source_entity, "option": option},
        )

    async def async_update_from_source(self, state) -> None:
        """Map the source select state to hvac_mode and target temperature."""
        # While a user adjustment is still being debounced, don't let the
        # (lagging) device value override what the user just set.
        if self._apply_cancel is not None:
            return
        option = state.state
        if option == OFF_OPTION or option in UNAVAILABLE_STATES:
            self._attr_hvac_mode = HVACMode.OFF
            # Keep the setpoint at the last heat level (in range 1-6) rather
            # than 0, so HomeKit shows where it'll resume and stays in range.
            self._attr_target_temperature = int(self._last_heat_level)
        else:
            self._attr_hvac_mode = HVACMode.HEAT
            try:
                self._attr_target_temperature = int(option)
                self._last_heat_level = option
            except (ValueError, TypeError):
                self._attr_target_temperature = int(self._last_heat_level)
        self._attr_current_temperature = self._attr_target_temperature
        self.async_write_ha_state()

# Kettles run well above HomeKit's usual thermostat range; these are only used
# when the target temperature helper doesn't declare its own min/max.
KETTLE_DEFAULT_MIN_TEMP = 40
KETTLE_DEFAULT_MAX_TEMP = 100

def _as_float(value, fallback: float) -> float:
    """Coerce a state attribute to a float, falling back when it's missing or junk."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback

class _ExtraSourcesMixin:
    """Track entities beyond the source, each feeding part of one climate state.

    Subclasses yield (entity_id, reader) pairs from `_extra_sources`. List the
    mixin before HomeKitDeviceEntity so its async_added_to_hass runs first.
    """

    def _extra_sources(self):
        """Yield the (entity_id, reader) pairs beyond the source entity."""
        return ()

    async def async_added_to_hass(self) -> None:
        """Track the extra entities alongside the source entity."""
        # Seed them before the base class writes state, so the first write is
        # complete rather than missing the parts they carry.
        for entity_id, reader in self._extra_sources():
            if state := self.hass.states.get(entity_id):
                reader(state)

        await super().async_added_to_hass()

        for entity_id, reader in self._extra_sources():
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [entity_id], self._make_handler(reader)
                )
            )

    def _make_handler(self, reader):
        """Build a state change handler that runs `reader` then writes state."""
        async def _handle(event: Event[EventStateChangedData]) -> None:
            new_state = event.data["new_state"]
            if new_state is None:
                return
            reader(new_state)
            self.async_write_ha_state()

        return _handle

class HomeKitKettleClimate(_ExtraSourcesMixin, HomeKitDeviceEntity, ClimateEntity):
    """Climate proxy presenting a kettle as a single HomeKit thermostat.

    The HomeKit bridge allocates one accessory per entity, so exposing power,
    current temperature and target temperature as three proxy entities gives
    three tiles in the Home app. Folding them into one climate entity gives one
    tile carrying all three as characteristics.
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_target_temperature_step = 1
    _attr_icon = "mdi:kettle"

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        power_entity: str,
        current_temp_entity: str | None = None,
        target_temp_entity: str | None = None,
    ) -> None:
        """Initialize the kettle proxy."""
        super().__init__(hass, entry_id, "Kettle", power_entity)
        # The kettle's primary entity, so it takes the device's name.
        self._attr_name = None
        self._current_temp_entity = current_temp_entity
        self._target_temp_entity = target_temp_entity

        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if target_temp_entity:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        self._attr_supported_features = features

        self._attr_min_temp = KETTLE_DEFAULT_MIN_TEMP
        self._attr_max_temp = KETTLE_DEFAULT_MAX_TEMP
        self._attr_hvac_mode = HVACMode.OFF

    def _extra_sources(self):
        """Yield the (entity_id, reader) pairs beyond the power entity."""
        for entity_id, reader in (
            (self._current_temp_entity, self._read_current_temp),
            (self._target_temp_entity, self._read_target_temp),
        ):
            if entity_id:
                yield entity_id, reader

    def _read_current_temp(self, state) -> None:
        """Read the measured water temperature."""
        if state.state in UNAVAILABLE_STATES:
            return
        try:
            self._attr_current_temperature = float(state.state)
        except (ValueError, TypeError):
            pass

    def _clamp(self, temp: float) -> float:
        """Hold a setpoint inside the helper's own range."""
        return min(max(temp, self._attr_min_temp), self._attr_max_temp)

    def _read_target_temp(self, state) -> None:
        """Read the setpoint and the range the helper allows."""
        # Range first, even when the state itself is unreadable: HA's HomeKit
        # bridge caches min/max when it builds the accessory, so a helper that
        # is briefly unavailable at startup must not leave the slider stuck on
        # the defaults for the rest of the session.
        attrs = state.attributes
        # A min of 0 is treated as "unset" by the bridge, which then substitutes
        # its own default and produces a broken range.
        min_temp = max(1, _as_float(attrs.get("min"), KETTLE_DEFAULT_MIN_TEMP))
        max_temp = _as_float(attrs.get("max"), KETTLE_DEFAULT_MAX_TEMP)
        if max_temp > min_temp:
            self._attr_min_temp = min_temp
            self._attr_max_temp = max_temp
        if (step := _as_float(attrs.get("step"), 0)) > 0:
            self._attr_target_temperature_step = step

        if state.state in UNAVAILABLE_STATES:
            return
        try:
            self._attr_target_temperature = self._clamp(float(state.state))
        except (ValueError, TypeError):
            pass

    async def async_set_temperature(self, **kwargs) -> None:
        """Push the setpoint to the target temperature helper."""
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None or not self._target_temp_entity:
            return
        # input_number and number both expose set_value(value), and both reject
        # an out-of-range value outright, so clamp rather than lose the write.
        await self.hass.services.async_call(
            self._target_temp_entity.split(".")[0],
            "set_value",
            {"entity_id": self._target_temp_entity, "value": self._clamp(temp)},
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Map Off/Heat onto the power entity."""
        # Generic turn_on/turn_off so a switch, input_boolean or fan all work.
        await self.hass.services.async_call(
            "homeassistant",
            "turn_off" if hvac_mode == HVACMode.OFF else "turn_on",
            {"entity_id": self._source_entity},
        )

    async def async_update_from_source(self, state) -> None:
        """Map the power entity state onto hvac mode and action."""
        if state.state in UNAVAILABLE_STATES:
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._attr_available = True
        heating = state.state == STATE_ON
        self._attr_hvac_mode = HVACMode.HEAT if heating else HVACMode.OFF
        self._attr_hvac_action = HVACAction.HEATING if heating else HVACAction.OFF
        self.async_write_ha_state()


# Climate features the air conditioner proxy passes straight through. Preset is
# left out: hOn's presets are programs, reached through the program select.
AC_MIRRORED_FEATURES = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    | ClimateEntityFeature.TARGET_HUMIDITY
    | ClimateEntityFeature.FAN_MODE
    | ClimateEntityFeature.TURN_ON
    | ClimateEntityFeature.TURN_OFF
)
SWING_SELECT_OPTION = "swing"
DEFAULT_VERTICAL_POSITION = "position_3"

def _hvac_mode(value) -> HVACMode | None:
    """Parse an HVAC mode, or None for anything HA doesn't define."""
    try:
        return HVACMode(value)
    except ValueError:
        return None

class HomeKitAirConditioner(_ExtraSourcesMixin, HomeKitDeviceEntity, ClimateEntity):
    """Climate proxy folding an air conditioner's extras into its tile.

    The bridge bridges this as a HeaterCooler (or a Thermostat, if chosen) with
    a linked fan service, but never offers fan-only as a target and swings on
    "both". This proxy maps fan on from Off to the unit's fan-only program,
    drives swing through the vertical louver select, and lists switches for
    the patched bridge override to add inside the same tile.
    """

    _attr_icon = "mdi:air-conditioner"
    _attr_swing_modes = [SWING_OFF, SWING_ON]
    # hOn applies a program before it takes follow-up writes; the same gap the
    # unit's hand-written fan automation used.
    _program_settle = 2

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        climate_entity: str,
        program_select: str | None = None,
        fan_only_option: str = DEFAULT_FAN_ONLY_OPTION,
        vertical_select: str | None = None,
        linked_switches: list[str] | None = None,
    ) -> None:
        """Initialize the air conditioner proxy."""
        super().__init__(hass, entry_id, "Air Conditioner", climate_entity)
        # The device's primary entity, so it takes the device's name.
        self._attr_name = None
        self._program_select = program_select
        self._fan_only_option = fan_only_option
        self._vertical_select = vertical_select
        self._linked_switches = list(linked_switches or [])
        self._dry_switch_unique_id = dry_mode_unique_id(entry_id, climate_entity)
        self._cmd_lock = asyncio.Lock()
        self._attr_temperature_unit = hass.config.units.temperature_unit
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_hvac_modes = [HVACMode.OFF]
        self._attr_fan_mode = FAN_OFF
        self._attr_swing_mode = SWING_OFF
        self._unit_off = True
        self._source_swing_modes: list[str] = []
        self._source_swing_mode: str | None = None
        self._vertical_position: str | None = None
        self._last_position = DEFAULT_VERTICAL_POSITION

    def _extra_sources(self):
        """Yield the vertical louver select when one is configured."""
        if self._vertical_select:
            yield self._vertical_select, self._read_vertical

    def _read_vertical(self, state) -> None:
        """Track the louver position, remembering the last fixed one."""
        if state.state in UNAVAILABLE_STATES:
            return
        self._vertical_position = state.state
        if state.state != SWING_SELECT_OPTION:
            self._last_position = state.state
        self._attr_swing_mode = self._current_swing()

    def _current_swing(self) -> str:
        """Whether the vanes are swinging, from whichever source drives them."""
        if self._vertical_select:
            swinging = self._vertical_position == SWING_SELECT_OPTION
        else:
            swinging = self._source_swing_mode not in (None, SWING_OFF)
        return SWING_ON if swinging else SWING_OFF

    @property
    def extra_state_attributes(self) -> dict | None:
        """List the switches the patched bridge adds inside this tile."""
        linked = list(self._linked_switches)
        registry = er.async_get(self.hass)
        if dry := registry.async_get_entity_id(
            "switch", DOMAIN, self._dry_switch_unique_id
        ):
            linked.append(dry)
        return {ATTR_HOMEKIT_LINKED_SWITCHES: linked} if linked else None

    async def async_update_from_source(self, state) -> None:
        """Mirror the source climate, reshaping fan and swing for HomeKit."""
        if state.state in UNAVAILABLE_STATES:
            self._attr_available = False
            self.async_write_ha_state()
            return

        self._attr_available = True
        attrs = state.attributes
        source_features = ClimateEntityFeature(attrs.get(ATTR_SUPPORTED_FEATURES, 0))
        features = source_features & AC_MIRRORED_FEATURES
        self._source_swing_modes = list(attrs.get(ATTR_SWING_MODES) or [])
        self._source_swing_mode = attrs.get(ATTR_SWING_MODE)
        if self._vertical_select or (
            source_features & ClimateEntityFeature.SWING_MODE
            and any(mode != SWING_OFF for mode in self._source_swing_modes)
        ):
            features |= ClimateEntityFeature.SWING_MODE
        self._attr_supported_features = features

        # Fan-only stays a real mode even though the bridge never offers it as
        # a target: the fan's Active reads off whenever the mode is off, so
        # reporting fan-only as off would show a running fan as stopped.
        modes = [
            mode
            for mode in map(_hvac_mode, attrs.get(ATTR_HVAC_MODES) or [])
            if mode is not None
        ]
        self._attr_hvac_modes = modes if HVACMode.OFF in modes else [HVACMode.OFF, *modes]
        self._unit_off = state.state == HVACMode.OFF
        mode = _hvac_mode(state.state)
        self._attr_hvac_mode = mode if mode in self._attr_hvac_modes else HVACMode.OFF
        self._attr_hvac_action = attrs.get(ATTR_HVAC_ACTION)

        self._attr_fan_modes = [FAN_OFF, *(attrs.get(ATTR_FAN_MODES) or [])]
        self._attr_fan_mode = (
            FAN_OFF if self._unit_off else attrs.get(ATTR_FAN_MODE, FAN_OFF)
        )
        self._attr_swing_mode = self._current_swing()

        self._attr_current_temperature = attrs.get(ATTR_CURRENT_TEMPERATURE)
        self._attr_target_temperature = attrs.get(ATTR_TEMPERATURE)
        self._attr_target_temperature_high = attrs.get(ATTR_TARGET_TEMP_HIGH)
        self._attr_target_temperature_low = attrs.get(ATTR_TARGET_TEMP_LOW)
        self._attr_current_humidity = attrs.get(ATTR_CURRENT_HUMIDITY)
        self._attr_target_humidity = attrs.get(ATTR_HUMIDITY)
        for attr, key in (
            ("_attr_min_temp", ATTR_MIN_TEMP),
            ("_attr_max_temp", ATTR_MAX_TEMP),
            ("_attr_target_temperature_step", ATTR_TARGET_TEMP_STEP),
        ):
            if (value := attrs.get(key)) is not None:
                setattr(self, attr, value)
        self.async_write_ha_state()

    async def _climate(self, service: str, **data) -> None:
        """Call a climate service on the source entity."""
        await self.hass.services.async_call(
            "climate",
            service,
            {"entity_id": self._source_entity, **data},
            blocking=True,
        )

    async def async_set_temperature(self, **kwargs) -> None:
        """Forward the setpoint (and any mode sent with it) to the source."""
        await self._climate("set_temperature", **kwargs)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Forward the mode to the source."""
        await self._climate("set_hvac_mode", hvac_mode=hvac_mode)

    async def async_turn_on(self) -> None:
        """Turn the source on."""
        await self._climate("turn_on")

    async def async_turn_off(self) -> None:
        """Turn the source off."""
        await self._climate("turn_off")

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Map the fan service's on/off and speed onto the unit.

        Off switches the whole unit off, as the fan is part of it. A speed
        while the unit is off starts fan-only first, the one way HomeKit can
        reach that mode.
        """
        async with self._cmd_lock:
            if fan_mode == FAN_OFF:
                await self._climate("set_hvac_mode", hvac_mode=HVACMode.OFF)
                return
            if self._unit_off:
                await self._start_fan_only()
            await self._climate("set_fan_mode", fan_mode=fan_mode)

    async def _start_fan_only(self) -> None:
        """Run the fan on its own with the vanes swinging."""
        if self._program_select:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": self._program_select, "option": self._fan_only_option},
                blocking=True,
            )
            await asyncio.sleep(self._program_settle)
        else:
            await self._climate("set_hvac_mode", hvac_mode=HVACMode.FAN_ONLY)
        await self._set_swing(True)

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        """Swing the vertical vanes, or hold them at the last fixed position."""
        async with self._cmd_lock:
            await self._set_swing(swing_mode != SWING_OFF)

    async def _set_swing(self, on: bool) -> None:
        """Drive the vertical louver select, or the source's own swing mode."""
        if self._vertical_select:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {
                    "entity_id": self._vertical_select,
                    "option": SWING_SELECT_OPTION if on else self._last_position,
                },
                blocking=True,
            )
            return
        if not on:
            await self._climate("set_swing_mode", swing_mode=SWING_OFF)
            return
        swinging = [mode for mode in self._source_swing_modes if mode != SWING_OFF]
        if not swinging:
            return
        # Vertical first: "both" asks for horizontal vanes many units lack.
        target = SWING_VERTICAL if SWING_VERTICAL in swinging else swinging[0]
        await self._climate("set_swing_mode", swing_mode=target)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the HomeKit Device climate proxies."""
    device_type = hass.data[DOMAIN][config_entry.entry_id]["device_type"]
    entities = []

    if device_type == "kettle":
        if power := config_entry.data.get(CONF_POWER_SWITCH):
            entities.append(
                HomeKitKettleClimate(
                    hass,
                    config_entry.entry_id,
                    power,
                    config_entry.data.get(CONF_CURRENT_TEMP),
                    config_entry.data.get(CONF_TARGET_TEMP),
                )
            )

    elif device_type == "electric_blanket":
        power = config_entry.data.get(CONF_POWER_SWITCH)
        zones = [z for z in (config_entry.data.get(CONF_ZONE_BODY),
                             config_entry.data.get(CONF_ZONE_FEET)) if z]
        for conf_key, label in ((CONF_ZONE_BODY, "Body"), (CONF_ZONE_FEET, "Feet")):
            if zone := config_entry.data.get(conf_key):
                entities.append(
                    HomeKitDeviceClimate(
                        hass,
                        config_entry.entry_id,
                        label,
                        zone,
                        power,
                        zones,
                    )
                )

    elif device_type == "air_conditioner":
        if climate_entity := config_entry.data.get(CONF_CLIMATE_ENTITY):
            entities.append(
                HomeKitAirConditioner(
                    hass,
                    config_entry.entry_id,
                    climate_entity,
                    config_entry.data.get(CONF_PROGRAM_SELECT),
                    config_entry.data.get(
                        CONF_FAN_ONLY_OPTION, DEFAULT_FAN_ONLY_OPTION
                    ),
                    config_entry.data.get(CONF_VERTICAL_DIRECTION),
                    config_entry.data.get(CONF_LINKED_SWITCHES),
                )
            )

    if entities:
        async_add_entities(entities)
