"""Tests for the air conditioner proxy and its dry mode switch.

The bridge gives a thermostat a linked fan service, but on its own it hides
fan-only whenever cool exists, swings on "both", and has no way to reach dry.
The proxy reshapes those, and lists switches for the patched bridge override to
put inside the same tile. The fixture mirrors a Haier hOn unit.
"""
from __future__ import annotations

import pytest
from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.helpers import entity_registry as er

from custom_components.homekit_device.climate import HomeKitAirConditioner

SOURCE = "climate.bedroom_air_conditioner"
PROGRAM = "select.bedroom_program"
VERTICAL = "select.bedroom_fan_direction_vertical"
NIGHT = "switch.bedroom_night_mode"
TIMER = "input_boolean.bedroom_ac_30_minutes_timer"
LINKED = "homekit_linked_switches"

# hOn: TARGET_TEMPERATURE | FAN_MODE | PRESET_MODE | SWING_MODE | TURN_OFF | TURN_ON
HON_FEATURES = 441
HON_MODES = ["off", "auto", "cool", "dry", "heat", "fan_only"]


def set_source(hass, state: str = HVACMode.OFF, **overrides) -> None:
    """Publish the source climate as hOn reports it."""
    hass.states.async_set(
        SOURCE,
        state,
        {
            "hvac_modes": HON_MODES,
            "min_temp": 16,
            "max_temp": 30,
            "target_temp_step": 1,
            "fan_modes": ["auto", "low", "medium", "high"],
            "swing_modes": ["off", "vertical", "horizontal", "both"],
            "current_temperature": 22.5,
            "temperature": 22.0,
            "fan_mode": "auto",
            "swing_mode": "off",
            ATTR_SUPPORTED_FEATURES: HON_FEATURES,
            **overrides,
        },
    )


@pytest.fixture(autouse=True)
def no_program_settle(monkeypatch):
    """Skip the wait hOn needs between a program and the next write."""
    monkeypatch.setattr(HomeKitAirConditioner, "_program_settle", 0)


@pytest.fixture
def air_conditioner(hass, setup_device, created_entities):
    """Set up an air conditioner and hand back (climate id, entry)."""

    async def _setup(source_state: str = HVACMode.OFF, **options):
        if source_state is not None:
            set_source(hass, source_state)
        config = {"climate_entity": SOURCE, **options}
        entry = await setup_device("air_conditioner", "Bedroom AC", **config)
        climates = created_entities(entry, "climate")
        assert len(climates) == 1, f"expected one thermostat, got {climates}"
        return climates[0], entry

    return _setup


def calls_to(service_calls, *entity_ids):
    """The forwarded calls aimed at the given source entities, in order."""
    found = []
    for call in service_calls:
        target = call.data.get("entity_id")
        targets = [target] if isinstance(target, str) else list(target or [])
        if any(entity_id in targets for entity_id in entity_ids):
            data = {k: v for k, v in call.data.items() if k != "entity_id"}
            found.append((call.domain, call.service, targets[0], data))
    return found


async def call_proxy(hass, service: str, entity_id: str, **data) -> None:
    await hass.services.async_call(
        "climate", service, {"entity_id": entity_id, **data}, blocking=True
    )


# Mirroring


async def test_mirrors_the_source_without_presets(hass, air_conditioner):
    """Presets are hOn programs, reached through the program select instead."""
    entity_id, _ = await air_conditioner(HVACMode.COOL)

    state = hass.states.get(entity_id)
    assert state.state == HVACMode.COOL
    assert state.attributes["hvac_modes"] == HON_MODES
    assert state.attributes["temperature"] == 22.0
    assert state.attributes["current_temperature"] == 22.5
    assert state.attributes["min_temp"] == 16
    assert state.attributes["max_temp"] == 30

    features = ClimateEntityFeature(state.attributes[ATTR_SUPPORTED_FEATURES])
    assert not features & ClimateEntityFeature.PRESET_MODE
    assert features & ClimateEntityFeature.FAN_MODE
    assert features & ClimateEntityFeature.SWING_MODE
    assert features & ClimateEntityFeature.TARGET_TEMPERATURE


async def test_features_the_source_lacks_are_not_asserted(hass, air_conditioner):
    """A source without fan or swing must not gain controls that would fail."""
    entity_id, _ = await air_conditioner(None)
    set_source(
        hass,
        HVACMode.COOL,
        **{ATTR_SUPPORTED_FEATURES: ClimateEntityFeature.TARGET_TEMPERATURE},
    )
    await hass.async_block_till_done()

    features = ClimateEntityFeature(
        hass.states.get(entity_id).attributes[ATTR_SUPPORTED_FEATURES]
    )
    assert features == ClimateEntityFeature.TARGET_TEMPERATURE


async def test_fan_only_reads_as_running_with_the_fan_on(hass, air_conditioner):
    """The bridge reads the fan's Active as off whenever the mode is off.

    So fan-only must stay fan-only: reported as off, a running fan would show
    as stopped and turning it on again would rerun the fan-only routine.
    """
    entity_id, _ = await air_conditioner(HVACMode.FAN_ONLY)

    state = hass.states.get(entity_id)
    assert state.state == HVACMode.FAN_ONLY
    assert state.attributes["fan_mode"] == "auto"


async def test_the_fan_reads_off_while_the_unit_is_off(hass, air_conditioner):
    entity_id, _ = await air_conditioner(HVACMode.OFF)

    state = hass.states.get(entity_id)
    assert state.attributes["fan_mode"] == "off"
    assert state.attributes["fan_modes"] == ["off", "auto", "low", "medium", "high"]


async def test_an_unavailable_source_makes_the_proxy_unavailable(hass, air_conditioner):
    entity_id, _ = await air_conditioner(HVACMode.COOL)
    hass.states.async_set(SOURCE, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


# Fan


async def test_fan_off_switches_the_unit_off(hass, air_conditioner, service_calls):
    entity_id, _ = await air_conditioner(HVACMode.COOL)

    await call_proxy(hass, "set_fan_mode", entity_id, fan_mode="off")

    assert calls_to(service_calls, SOURCE) == [
        ("climate", "set_hvac_mode", SOURCE, {"hvac_mode": HVACMode.OFF}),
    ]


async def test_fan_on_from_off_starts_fan_only_then_sets_the_speed(
    hass, air_conditioner, service_calls
):
    """Same order as the unit's hand-written routine: program, swing, speed."""
    hass.states.async_set(VERTICAL, "position_3", {"options": ["position_3", "swing"]})
    entity_id, _ = await air_conditioner(
        HVACMode.OFF, program_select=PROGRAM, vertical_direction=VERTICAL
    )

    await call_proxy(hass, "set_fan_mode", entity_id, fan_mode="medium")

    assert calls_to(service_calls, SOURCE, PROGRAM, VERTICAL) == [
        ("select", "select_option", PROGRAM, {"option": "iot_fan"}),
        ("select", "select_option", VERTICAL, {"option": "swing"}),
        ("climate", "set_fan_mode", SOURCE, {"fan_mode": "medium"}),
    ]


async def test_fan_only_falls_back_to_the_hvac_mode(hass, air_conditioner, service_calls):
    """Without a program select, the climate's own fan_only mode is used."""
    entity_id, _ = await air_conditioner(HVACMode.OFF)

    await call_proxy(hass, "set_fan_mode", entity_id, fan_mode="low")

    assert calls_to(service_calls, SOURCE) == [
        ("climate", "set_hvac_mode", SOURCE, {"hvac_mode": HVACMode.FAN_ONLY}),
        ("climate", "set_swing_mode", SOURCE, {"swing_mode": "vertical"}),
        ("climate", "set_fan_mode", SOURCE, {"fan_mode": "low"}),
    ]


async def test_a_speed_while_running_only_changes_the_speed(
    hass, air_conditioner, service_calls
):
    entity_id, _ = await air_conditioner(HVACMode.COOL, program_select=PROGRAM)

    await call_proxy(hass, "set_fan_mode", entity_id, fan_mode="high")

    assert calls_to(service_calls, SOURCE, PROGRAM) == [
        ("climate", "set_fan_mode", SOURCE, {"fan_mode": "high"}),
    ]


# Swing


async def test_swing_drives_the_vertical_select_and_restores_the_position(
    hass, air_conditioner, service_calls
):
    options = {"options": ["position_1", "position_2", "position_3", "swing"]}
    hass.states.async_set(VERTICAL, "position_2", options)
    entity_id, _ = await air_conditioner(HVACMode.COOL, vertical_direction=VERTICAL)
    assert hass.states.get(entity_id).attributes["swing_mode"] == "off"

    await call_proxy(hass, "set_swing_mode", entity_id, swing_mode="on")
    hass.states.async_set(VERTICAL, "swing", options)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).attributes["swing_mode"] == "on"

    await call_proxy(hass, "set_swing_mode", entity_id, swing_mode="off")

    assert calls_to(service_calls, VERTICAL) == [
        ("select", "select_option", VERTICAL, {"option": "swing"}),
        ("select", "select_option", VERTICAL, {"option": "position_2"}),
    ]


async def test_swing_without_a_select_prefers_vertical_over_both(
    hass, air_conditioner, service_calls
):
    """The bridge's own pick is "both", which needs horizontal vanes."""
    entity_id, _ = await air_conditioner(HVACMode.COOL)

    await call_proxy(hass, "set_swing_mode", entity_id, swing_mode="on")

    assert calls_to(service_calls, SOURCE) == [
        ("climate", "set_swing_mode", SOURCE, {"swing_mode": "vertical"}),
    ]


# Linked switches


async def test_linked_switches_list_the_configured_ones_and_dry(hass, air_conditioner):
    entity_id, entry = await air_conditioner(
        HVACMode.COOL, linked_switches=[NIGHT, TIMER]
    )
    # Any source update rewrites the state after every platform is set up.
    set_source(hass, HVACMode.COOL)
    await hass.async_block_till_done()

    dry = er.async_get(hass).async_get_entity_id(
        "switch", "homekit_device", f"homekit_device_{entry.entry_id}_{SOURCE}_dry_mode"
    )
    assert dry is not None
    assert hass.states.get(entity_id).attributes[LINKED] == [NIGHT, TIMER, dry]


async def test_no_linked_attribute_without_switches_or_dry(hass, air_conditioner):
    """Stock climates stay untouched by the bridge override."""
    set_source(hass, HVACMode.COOL, hvac_modes=["off", "cool", "heat"])
    entity_id, _ = await air_conditioner(None)

    assert LINKED not in hass.states.get(entity_id).attributes


# Dry mode switch


async def test_dry_switch_is_categorised_and_reads_the_mode(
    hass, air_conditioner, created_entities
):
    _, entry = await air_conditioner(HVACMode.DRY)

    switches = created_entities(entry, "switch")
    assert len(switches) == 1
    registry_entry = er.async_get(hass).async_get(switches[0])
    assert registry_entry.entity_category is EntityCategory.CONFIG
    assert hass.states.get(switches[0]).state == "on"


async def test_dry_switch_only_exists_when_the_source_has_dry(
    hass, air_conditioner, created_entities
):
    set_source(hass, HVACMode.COOL, hvac_modes=["off", "cool", "heat"])
    _, entry = await air_conditioner(None)

    assert created_entities(entry, "switch") == []


async def test_dry_switch_is_kept_while_the_source_is_still_loading(
    hass, air_conditioner, created_entities
):
    """hOn may come up after this integration; the switch must not be lost."""
    _, entry = await air_conditioner(None)

    assert len(created_entities(entry, "switch")) == 1


async def test_dry_off_restores_the_previous_mode(
    hass, air_conditioner, created_entities, service_calls
):
    _, entry = await air_conditioner(HVACMode.HEAT)
    dry = created_entities(entry, "switch")[0]

    await hass.services.async_call("switch", "turn_on", {"entity_id": dry}, blocking=True)
    set_source(hass, HVACMode.DRY)
    await hass.async_block_till_done()
    await hass.services.async_call("switch", "turn_off", {"entity_id": dry}, blocking=True)

    assert calls_to(service_calls, SOURCE) == [
        ("climate", "set_hvac_mode", SOURCE, {"hvac_mode": HVACMode.DRY}),
        ("climate", "set_hvac_mode", SOURCE, {"hvac_mode": HVACMode.HEAT}),
    ]


async def test_dry_off_leaves_other_modes_alone(
    hass, air_conditioner, created_entities, service_calls
):
    _, entry = await air_conditioner(HVACMode.COOL)
    dry = created_entities(entry, "switch")[0]

    await hass.services.async_call("switch", "turn_off", {"entity_id": dry}, blocking=True)

    assert calls_to(service_calls, SOURCE) == []
