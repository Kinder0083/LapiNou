# SPDX-License-Identifier: MIT
import pytest

from lapinou.config import SafetyConfig
from lapinou.tools.policy import SERVICES, Allow, Deny, NeedsConfirmation, Policy


def policy(**kw) -> Policy:
    return Policy(SafetyConfig(**kw))


def test_lock_unlock_denied_by_default():
    assert isinstance(policy().check("lock", "unlock", "lock.porte_entree"), Deny)


def test_lock_unlock_needs_confirmation_when_enabled():
    decision = policy(allowed_entities=["lock.porte_entree"]).check(
        "lock", "unlock", "lock.porte_entree", friendly_name="Porte d'entrée"
    )
    assert isinstance(decision, NeedsConfirmation)
    assert "Porte d'entrée" in decision.phrase
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


def test_scene_and_script_allowed_once_contents_checked():
    p = policy()
    assert isinstance(p.check("scene", "turn_on", "scene.soiree", linked_reason=None), Allow)
    assert isinstance(
        p.check("script", "turn_on", "script.bonne_nuit", linked_reason=None), Allow
    )


def test_scene_and_script_unchecked_or_sensitive_contents_denied():
    p = policy()
    assert isinstance(p.check("scene", "turn_on", "scene.soiree"), Deny)  # contenu non lu
    assert isinstance(
        p.check("script", "turn_on", "script.x", linked_reason="appelle lock.porte"), Deny
    )


def test_script_service_name_bypass_denied():
    # HA lancerait script.ouvrir_garage : le service doit être dans la liste du domaine.
    assert isinstance(
        policy().check("script", "ouvrir_garage", "script.bonne_nuit", linked_reason=None), Deny
    )


@pytest.mark.parametrize("domain,service", [
    ("light", "reload"), ("switch", "reload"), ("scene", "apply"), ("scene", "create"),
    ("script", "reload"), ("cover", "toggle"), ("climate", "set_hvac_mode"),
    ("media_player", "play_media"), ("lock", "frobnicate"),
])
def test_services_outside_allow_list_denied(domain, service):
    p = policy(allowed_entities=[f"{domain}.x"])
    assert isinstance(
        p.check(domain, service, f"{domain}.x", device_class="shutter", linked_reason=None), Deny
    )


def test_service_lists():
    assert SERVICES["light"] == {"turn_on", "turn_off", "toggle"}
    assert SERVICES["script"] == {"turn_on", "toggle"}
    assert SERVICES["scene"] == {"turn_on"}
    assert SERVICES["media_player"] == {"volume_set", "media_play", "media_pause", "media_stop",
                                        "media_next_track"}


def test_domain_not_allowed_is_denied():
    assert isinstance(policy().check("homeassistant", "restart", "homeassistant.x"), Deny)
    assert isinstance(policy().check("automation", "turn_off", "automation.reveil"), Deny)


def test_domain_must_match_entity():
    assert isinstance(policy().check("light", "turn_on", "lock.porte_entree"), Deny)


@pytest.mark.parametrize("entity", ["", "all", "light.salon,lock.porte"])
def test_malformed_entity_denied(entity):
    assert isinstance(policy().check("light", "turn_on", entity), Deny)


def test_garage_gate_door_covers_are_sensitive():
    p = policy()
    assert isinstance(p.check("cover", "open_cover", "cover.g", device_class="garage"), Deny)
    assert isinstance(p.check("cover", "open_cover", "cover.p", device_class="gate"), Deny)
    assert isinstance(p.check("cover", "open_cover", "cover.e", device_class="door"), Deny)
    assert isinstance(
        p.check("cover", "open_cover", "cover.volet", device_class="shutter"), Allow
    )


def test_cover_without_device_class_fails_closed():
    assert isinstance(policy().check("cover", "open_cover", "cover.inconnu"), Deny)


def test_marked_sensitive_entity():
    p = policy(sensitive_entities=["switch.prise_cuisine"])
    assert isinstance(p.check("switch", "turn_on", "switch.prise_cuisine"), Deny)
    assert isinstance(p.check("switch", "turn_on", "switch.lampe"), Allow)


@pytest.mark.parametrize("entity,name", [
    ("switch.chauffe_eau", None),
    ("switch.four_cuisine", None),
    ("switch.plaques", None),
    ("light.lampe_jardin", "Lumière du portail"),
    ("switch.prise", "Radiateur chambre"),
    ("light.entree", "Porte d'entrée"),
])
def test_sensitive_stems_and_words_in_id_or_name(entity, name):
    assert isinstance(
        policy().check(entity.split(".")[0], "turn_on", entity, friendly_name=name), Deny
    )


@pytest.mark.parametrize("entity,name", [
    ("switch.fourchette", None), ("light.apporte", "Lampe qui apporte la joie"),
])
def test_words_match_whole_words_only(entity, name):
    assert isinstance(
        policy().check(entity.split(".")[0], "turn_on", entity, friendly_name=name), Allow
    )


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
    assert policy().refuses("non merci") and not policy().refuses("oui")
    with_pin = policy(pin="1234")
    assert not with_pin.confirms("oui")
    assert with_pin.confirms("oui, 1 2 3 4")
    assert with_pin.confirms("un deux trois quatre")
    assert not with_pin.confirms("oui 4321")


@pytest.mark.parametrize("utterance", [
    "zéro un deux trois quatre cinq six sept huit neuf",
    "1 2 3 4 5",
    "0 1 2 3 4",
    "12345",
    "un deux trois",
])
def test_pin_needs_exact_sequence(utterance):
    assert not policy(pin="1234").confirms(utterance)
