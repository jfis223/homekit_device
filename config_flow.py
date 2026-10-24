"""Config flow for HomeKit Device Aggregator integration."""
from typing import Any, Dict, Optional
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN,
    CONF_NAME,
    CONF_DEVICE_TYPE,
    CONF_POWER_SWITCH,
    CONF_TEMP_SENSORS,
    CONF_TARGET_TEMP,
    CONF_CURRENT_TEMP,
    CONF_STATUS_SENSOR,
    CONF_COUNTDOWN,
    CONF_FAULT,
    CONF_KEEP_WARM,
    CONF_SPEED_CONTROL,
    CONF_OSCILLATION,
    CONF_DIRECTION,
    CONF_BRIGHTNESS,
    CONF_COLOR_TEMP,
    CONF_RGB_CONTROL,
    CONF_CURRENT_HUMIDITY,
    CONF_TARGET_HUMIDITY,
    CONF_WATER_LEVEL,
    CONF_AIR_QUALITY,
    CONF_FILTER_LIFE,
    CONF_PM25,
    CONF_VOC,
    CONF_DOOR_POSITION,
    CONF_OBSTRUCTION,
    CONF_MOTION,
    CONF_LIGHT_SWITCH,
    CONF_ALARM_STATE,
    CONF_SENSORS,
    CONF_SIREN,
    CONF_LASER_LIGHT,
    CONF_BACKGROUND_LIGHT,
    CONF_ROTATION_FAN,
    CONF_CHILD_LOCK,
    CONF_DISPLAY_SWITCH,
    CONF_ZONE_BODY,
    CONF_ZONE_FEET,
    CONF_BODY_TIMER,
    CONF_FEET_TIMER,
    CONF_LIFT_COVER,
    CONF_TILT_COVER,
    CONF_CLIMATE_ENTITY,
    CONF_PROGRAM_SELECT,
    CONF_FAN_ONLY_OPTION,
    CONF_VERTICAL_DIRECTION,
    CONF_LINKED_SWITCHES,
    DEFAULT_FAN_ONLY_OPTION,
    DEVICE_TYPES,
    DEFAULT_NAME,
)

# Device types whose sources already carry power, so the flow must not demand a
# power switch for them.
NO_POWER_TYPES = ("shutter", "air_conditioner", "climate_switches")

# The air conditioner fields that can change after setup. The climate entity is
# not among them: the entry's entities are keyed on it.
AIR_CONDITIONER_OPTIONS = {
    vol.Optional(CONF_PROGRAM_SELECT): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="select")
    ),
    vol.Optional(CONF_FAN_ONLY_OPTION, default=DEFAULT_FAN_ONLY_OPTION): str,
    vol.Optional(CONF_VERTICAL_DIRECTION): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="select")
    ),
    # The list order is the order of the switches in the HomeKit tile.
    vol.Optional(CONF_LINKED_SWITCHES): selector.EntitySelector(
        selector.EntitySelectorConfig(
            domain=["switch", "input_boolean"],
            multiple=True,
        )
    ),
}
CLIMATE_SWITCHES_OPTIONS = {
    vol.Optional(CONF_LINKED_SWITCHES): AIR_CONDITIONER_OPTIONS[
        vol.Optional(CONF_LINKED_SWITCHES)
    ],
}
# The fields each type can change after setup, keyed by the options step that
# shows them.
OPTIONS = {
    "air_conditioner": ("init", AIR_CONDITIONER_OPTIONS),
    "climate_switches": ("switches", CLIMATE_SWITCHES_OPTIONS),
}

class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HomeKit Device Aggregator."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize config flow."""
        self._data: Dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Edit an air conditioner or a climate's switches after setup."""
        return AirConditionerOptionsFlow()

    @classmethod
    @callback
    def async_supports_options_flow(
        cls, config_entry: config_entries.ConfigEntry
    ) -> bool:
        """Only the climate types with switches have settings worth changing."""
        return config_entry.data.get(CONF_DEVICE_TYPE) in OPTIONS

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_device_config()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Required(CONF_DEVICE_TYPE): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(DEVICE_TYPES.keys()),
                            translation_key="device_type",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        ),
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_device_config(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle device specific configuration."""
        errors = {}

        if user_input is not None:
            if self._invalid_tilt_cover(user_input):
                errors[CONF_TILT_COVER] = "tilt_same_as_lift"
            else:
                self._data.update(user_input)
                return self.async_create_entry(
                    title=self._data[CONF_NAME],
                    data=self._data,
                )

        schema = self._get_device_schema()
        return self.async_show_form(
            step_id="device_config",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), user_input or {}
            ),
            errors=errors,
        )

    def _invalid_tilt_cover(self, user_input: Dict[str, Any]) -> bool:
        """Reject one entity used for both halves unless it has its own tilt."""
        tilt = user_input.get(CONF_TILT_COVER)
        if not tilt or tilt != user_input.get(CONF_LIFT_COVER):
            return False
        state = self.hass.states.get(tilt)
        features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0) if state else 0
        return not features & CoverEntityFeature.SET_TILT_POSITION

    def _get_device_schema(self) -> dict:
        """Get the configuration schema for the selected device type."""
        device_type = self._data[CONF_DEVICE_TYPE]
        base_schema = {
            vol.Required(CONF_POWER_SWITCH): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["switch", "fan"])
            ),
            vol.Optional(CONF_STATUS_SENSOR): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
        }

        device_schemas = {
            "kettle": {
                vol.Optional(CONF_CURRENT_TEMP): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_TARGET_TEMP): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["input_number", "number"])
                ),
                vol.Optional(CONF_COUNTDOWN): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_FAULT): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_KEEP_WARM): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["switch", "input_boolean", "select", "input_select"]
                    )
                ),
            },
            "thermostat": {
                vol.Required(CONF_CURRENT_TEMP): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_TARGET_TEMP): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
                vol.Optional(CONF_TEMP_SENSORS): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="sensor",
                        multiple=True,
                    )
                ),
            },
            "fan": {
                vol.Optional(CONF_SPEED_CONTROL): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
                vol.Optional(CONF_OSCILLATION): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Optional(CONF_DIRECTION): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="select")
                ),
            },
            "light": {
                vol.Optional(CONF_BRIGHTNESS): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
                vol.Optional(CONF_COLOR_TEMP): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
                vol.Optional(CONF_RGB_CONTROL): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="text")
                ),
            },
            "humidifier": {
                vol.Required(CONF_CURRENT_HUMIDITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(CONF_TARGET_HUMIDITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="number")
                ),
                vol.Optional(CONF_WATER_LEVEL): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
            },
            "air_purifier": {
                vol.Optional(CONF_AIR_QUALITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_FILTER_LIFE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_PM25): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_VOC): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_CHILD_LOCK): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Optional(CONF_DISPLAY_SWITCH): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
            },
            "garage_door": {
                vol.Required(CONF_DOOR_POSITION): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="cover")
                ),
                vol.Optional(CONF_OBSTRUCTION): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
                vol.Optional(CONF_MOTION): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="binary_sensor")
                ),
                vol.Optional(CONF_LIGHT_SWITCH): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="light")
                ),
            },
            "security_system": {
                vol.Required(CONF_ALARM_STATE): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="alarm_control_panel")
                ),
                vol.Optional(CONF_SENSORS): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain="binary_sensor",
                        multiple=True,
                    )
                ),
                vol.Optional(CONF_SIREN): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
            },
            "star_projector": {
                vol.Optional(CONF_LASER_LIGHT): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="light")
                ),
                vol.Optional(CONF_BACKGROUND_LIGHT): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="light")
                ),
                vol.Optional(CONF_ROTATION_FAN): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="fan")
                ),
            },
            "shutter": {
                vol.Required(CONF_LIFT_COVER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="cover")
                ),
                vol.Optional(CONF_TILT_COVER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="cover")
                ),
            },
            "electric_blanket": {
                vol.Required(CONF_ZONE_BODY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="select")
                ),
                vol.Optional(CONF_ZONE_FEET): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="select")
                ),
                vol.Optional(CONF_BODY_TIMER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="select")
                ),
                vol.Optional(CONF_FEET_TIMER): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="select")
                ),
            },
            "air_conditioner": {
                vol.Required(CONF_CLIMATE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
                **AIR_CONDITIONER_OPTIONS,
            },
            "climate_switches": {
                vol.Required(CONF_CLIMATE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate")
                ),
                **CLIMATE_SWITCHES_OPTIONS,
            },
        }

        # Shutters and the climate types have no power entity - the covers and
        # the climate entity are the whole device.
        schema = {} if device_type in NO_POWER_TYPES else base_schema.copy()
        if device_type in device_schemas:
            schema.update(device_schemas[device_type])

        return schema


class AirConditionerOptionsFlow(config_entries.OptionsFlow):
    """Change a climate's switches, their order and (for an AC) its selects.

    The values go into the entry's data, which every platform reads, and the
    entry reloads. A new switch list then rebuilds the HomeKit accessory.
    """

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Show the air conditioner's changeable fields, or route to the switches."""
        step_id, _ = OPTIONS[self.config_entry.data[CONF_DEVICE_TYPE]]
        if step_id != "init":
            return await self.async_step_switches(user_input)
        return self._edit("init", user_input)

    async def async_step_switches(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Show a climate's switch list."""
        return self._edit("switches", user_input)

    def _edit(self, step_id: str, user_input: Optional[Dict[str, Any]]) -> FlowResult:
        entry = self.config_entry
        _, options = OPTIONS[entry.data[CONF_DEVICE_TYPE]]
        if user_input is not None:
            # A field cleared in the form is absent from user_input, so drop
            # every changeable key before applying the submitted ones.
            data = {
                key: value
                for key, value in entry.data.items()
                if key not in {str(marker) for marker in options}
            }
            self.hass.config_entries.async_update_entry(
                entry, data={**data, **user_input}
            )
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(options), dict(entry.data)
            ),
        )
