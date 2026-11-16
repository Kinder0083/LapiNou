# SPDX-License-Identifier: MIT
import pytest

from lapinou.config import SafetyConfig
from lapinou.tools.policy import Allow, Deny, NeedsConfirmation, Policy


def policy(**kw) -> Policy:
    return Policy(SafetyConfig(**kw))


def test_lock_unlock_denied_by_default():
    assert isinstance(policy().check("lock", "unlock", "lock.porte_entree"), Deny)


def test_lock_unlock_needs_confirmation_when_enabled():
    decision = policy(allowed_entities=["lock.porte_entree"]).check(
        "lock", "unlock", "lock.porte_entree"
    )
    assert isinstance(decision, NeedsConfirmation)
    assert decision.phrase
    assert not decision.needs_pin


def test_confirmation_asks_for_pin_when_set():
    decision = policy(allowed_entities=["lock.porte_entree"], pin="1234").check(
        "lock", "unlock", "lock.porte_entree"
    )
    assert isinstance(decision, NeedsConfirmation) and decision.needs_pin


def test_alarm_denied_by_default():
    assert isinstance(
        policy().check("alarm_control_panel", "alarm_disarm", "alarm_control_panel.maison"), Deny
    )


def test_light_allowed():
    assert isinstance(policy().check("light", "turn_on", "light.salon"), Allow)


def test_scene_and_script_allowed():
    assert isinstance(policy().check("scene", "turn_on", "scene.soiree"), Allow)
    assert isinstance(policy().check("script", "turn_on", "script.bonne_nuit"), Allow)


def test_domain_not_allowed_is_denied():
    assert isinstance(policy().check("homeassistant", "restart", "homeassistant.x"), Deny)
    assert isinstance(policy().check("automation", "turn_off", "automation.reveil"), Deny)


def test_domain_must_match_entity():
    assert isinstance(policy().check("light", "turn_on", "lock.porte_entree"), Deny)


@pytest.mark.parametrize("entity", ["", "all", "light.salon,lock.porte"])
def test_malformed_entity_denied(entity):
    assert isinstance(policy().check("light", "turn_on", entity), Deny)


def test_garage_and_gate_covers_are_sensitive():
    p = policy()
    assert isinstance(p.check("cover", "open_cover", "cover.garage", device_class="garage"), Deny)
    assert isinstance(p.check("cover", "open_cover", "cover.portail", device_class="gate"), Deny)
    assert isinstance(
        p.check("cover", "open_cover", "cover.volet", device_class="shutter"), Allow
    )


def test_cover_without_device_class_fails_closed():
    assert isinstance(policy().check("cover", "open_cover", "cover.inconnu"), Deny)


def test_marked_sensitive_entity():
    p = policy(sensitive_entities=["switch.four"])
    assert isinstance(p.check("switch", "turn_on", "switch.four"), Deny)
    assert isinstance(p.check("switch", "turn_on", "switch.lampe"), Allow)


def test_climate_range():
    p = policy()
    assert isinstance(
        p.check("climate", "set_temperature", "climate.salon", data={"temperature": 20}), Allow
    )
    assert isinstance(
        p.check("climate", "set_temperature", "climate.salon", data={"temperature": 28}), Deny
    )
    assert isinstance(
        p.check("climate", "set_temperature", "climate.salon", data={"temperature": 12}), Deny
    )
    enabled = policy(allowed_entities=["climate.salon"])
    assert isinstance(
        enabled.check("climate", "set_temperature", "climate.salon", data={"temperature": 28}),
        NeedsConfirmation,
    )


def test_confirms_yes_and_pin():
    assert policy().confirms("oui, vas-y")
    assert not policy().confirms("non merci")
    assert not policy().confirms("peut-être")
    with_pin = policy(pin="1234")
    assert not with_pin.confirms("oui")
    assert with_pin.confirms("oui, 1 2 3 4")
    assert with_pin.confirms("un deux trois quatre")
    assert not with_pin.confirms("oui 4321")
