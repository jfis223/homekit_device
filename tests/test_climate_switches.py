"""Tests for the climate_switches proxy.

It exists so a climate with nothing to reshape, like a boiler, can still carry
switches into its tile through the patched bridge override. So it must mirror
the source exactly and add none of the air conditioner's extras.
"""
from __future__ import annotations

import pytest
from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_SUPPORTED_FEATURES

SOURCE = "climate.heater_boiler"
TIMERS = ["input_boolean.boiler_1_hour_timer", "input_boolean.boiler_2_hour_timer"]
LINKED = "homekit_linked_switches"

# Ariston: TARGET_TEMPERATURE | TURN_OFF | TURN_ON
BOILER_FEATURES = 385


def set_source(hass, state: str = HVACMode.HEAT) -> None:
    hass.states.async_set(
        SOURCE,
        state,
        {
            "hvac_modes": ["heat", "off"],
            "min_temp": 7,
            "max_temp": 35,
            "current_temperature": 21.0,
            "temperature": 24.5,
            ATTR_SUPPORTED_FEATURES: BOILER_FEATURES,
        },
    )


@pytest.fixture
def boiler(hass, setup_device, created_entities):
    """Set up a boiler proxy and hand back (entity id, entry)."""

    async def _setup(**options):
        set_source(hass)
        entry = await setup_device(
            "climate_switches", "Heater Boiler", climate_entity=SOURCE, **options
        )
        climates = created_entities(entry, "climate")
        assert len(climates) == 1, f"expected one thermostat, got {climates}"
        return climates[0], entry

    return _setup


async def test_mirrors_the_source_without_extras(hass, boiler, created_entities):
    """No fan, swing or dry switch: the bridge must see a plain thermostat."""
    entity_id, entry = await boiler(linked_switches=TIMERS)

    state = hass.states.get(entity_id)
    assert state.state == HVACMode.HEAT
    assert state.attributes["hvac_modes"] == ["heat", "off"]
    assert state.attributes["temperature"] == 24.5
    assert state.attributes["current_temperature"] == 21.0
    features = ClimateEntityFeature(state.attributes[ATTR_SUPPORTED_FEATURES])
    assert features == ClimateEntityFeature(BOILER_FEATURES)
    assert created_entities(entry, "switch") == []


async def test_lists_the_switches_in_order(hass, boiler):
    entity_id, _ = await boiler(linked_switches=list(reversed(TIMERS)))

    assert hass.states.get(entity_id).attributes[LINKED] == list(reversed(TIMERS))


async def test_no_switches_no_attribute(hass, boiler):
    entity_id, _ = await boiler()

    assert LINKED not in hass.states.get(entity_id).attributes


async def test_commands_reach_the_source(hass, boiler, service_calls):
    entity_id, _ = await boiler(linked_switches=TIMERS)

    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": entity_id, "temperature": 22}, blocking=True
    )
    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": "off"}, blocking=True
    )

    forwarded = [
        (c.service, {k: v for k, v in c.data.items() if k != "entity_id"})
        for c in service_calls
        if c.data.get("entity_id") == SOURCE
    ]
    assert forwarded == [
        ("set_temperature", {"temperature": 22}),
        ("set_hvac_mode", {"hvac_mode": "off"}),
    ]
