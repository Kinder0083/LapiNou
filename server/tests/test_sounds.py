# SPDX-License-Identifier: MIT
"""Les sons reconnus d'un enregistrement : catégories françaises et alerte la plus grave."""

from __future__ import annotations

from lapinou.voice import sounds


def test_categories_in_french_most_likely_first_and_the_gravest_alert():
    tags = [("Bark", 0.8), ("Speech", 0.6), ("Shatter", 0.4), ("Music", 0.1)]
    s = sounds.describe(tags, 0.3)
    assert s.sons == ["chien", "parole", "verre brisé"]
    assert s.alerte == "verre brisé"  # le verre passe avant le chien
    assert s.etiquettes == [("Bark", 0.8), ("Speech", 0.6), ("Shatter", 0.4)]


def test_unlikely_sounds_are_left_out_and_no_alert_without_one():
    s = sounds.describe([("Speech", 0.9), ("Glass", 0.2), ("Television", 0.35)], 0.3)
    assert s.sons == ["parole", "télévision"]
    assert s.alerte == ""


def test_several_labels_of_one_category_count_once():
    s = sounds.describe([("Smoke detector, smoke alarm", 0.7), ("Alarm", 0.6), ("Buzzer", 0.5)],
                        0.3)
    assert s.sons == ["alarme"]
    assert s.alerte == "alarme"


def test_nothing_heard():
    s = sounds.describe([], 0.3)
    assert (s.sons, s.alerte, s.etiquettes) == ([], "", [])


def test_every_alert_is_a_category():
    assert set(sounds.ALERTS) <= set(sounds.CATEGORIES)


def test_model_present_needs_the_model_and_its_labels(tmp_path):
    assert not sounds.model_present(tmp_path)
    (tmp_path / sounds.MODEL_DIR).mkdir()
    (tmp_path / sounds.MODEL).write_bytes(b"x")
    assert not sounds.model_present(tmp_path)
    (tmp_path / sounds.LABELS).write_text("index,mid,display_name\n")
    assert sounds.model_present(tmp_path)
