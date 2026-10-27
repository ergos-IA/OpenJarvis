"""Contact search: accents, word order, partial names, typos, unsaved senders."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjarvis.channels.whatsapp_store import WhatsAppStore
from openjarvis.tools.whatsapp_tools import _resolve_recipient


@pytest.fixture
def store(tmp_path):
    s = WhatsAppStore(tmp_path / "wa.db")
    s.upsert_contact(
        "34600000001@s.whatsapp.net", phone="34600000001", name="Óscar Peña"
    )
    s.upsert_contact(
        "34600000002@s.whatsapp.net", phone="34600000002", name="Oscar Martín"
    )
    s.upsert_contact(
        "34600000003@s.whatsapp.net", phone="34600000003", name="Ana López"
    )
    s.upsert_contact(
        "34600000004@s.whatsapp.net", phone="34600000004", name="Peña Fontanero"
    )
    yield s
    s.close()


def names(found):
    return [c["name"] or c["push_name"] for c in found]


class TestSearch:
    def test_without_accents(self, store):
        assert names(store.search_contacts("oscar peña"))[0] == "Óscar Peña"
        assert names(store.search_contacts("OSCAR PENA"))[0] == "Óscar Peña"

    def test_word_order_and_partial(self, store):
        assert names(store.search_contacts("peña oscar"))[0] == "Óscar Peña"
        assert names(store.search_contacts("osc pe"))[0] == "Óscar Peña"

    def test_typo(self, store):
        assert names(store.search_contacts("osacr peña"))[0] == "Óscar Peña"

    def test_first_name_lists_all_matches(self, store):
        assert set(names(store.search_contacts("oscar"))) == {
            "Óscar Peña",
            "Oscar Martín",
        }

    def test_every_word_must_match(self, store):
        assert names(store.search_contacts("oscar lopez")) == []

    def test_number(self, store):
        assert names(store.search_contacts("600 000 003")) == ["Ana López"]

    def test_unsaved_sender_found_from_messages(self, store):
        store.add_message(
            {
                "message_id": "m1",
                "chat_jid": "34611222333@s.whatsapp.net",
                "sender_jid": "34611222333@s.whatsapp.net",
                "sender_name": "Lucía Gómez",
                "phone": "34611222333",
                "text": "hola",
            }
        )
        [c] = store.search_contacts("lucia")
        assert c["push_name"] == "Lucía Gómez" and c["phone"] == "34611222333"


class TestResolve:
    def ch(self, store):
        return SimpleNamespace(store=store)

    def test_full_name_is_sure(self, store):
        r = _resolve_recipient(self.ch(store), "oscar peña")
        assert r["jid"] == "34600000001@s.whatsapp.net" and r["sure"] is True

    def test_ambiguous_first_name_asks(self, store):
        r = _resolve_recipient(self.ch(store), "Oscar")
        assert len(r["choices"]) == 2

    def test_typo_is_not_sure(self, store):
        r = _resolve_recipient(self.ch(store), "osacr peña")
        assert r["name"] == "Óscar Peña" and r["sure"] is False

    def test_unknown_number_is_not_sure(self, store):
        r = _resolve_recipient(self.ch(store), "611 999 888")
        assert r["jid"] == "34611999888@s.whatsapp.net" and r["sure"] is False

    def test_saved_number_is_sure(self, store):
        r = _resolve_recipient(self.ch(store), "600000003")
        assert r["name"] == "Ana López" and r["sure"] is True

    def test_not_found_suggests_retry(self, store):
        r = _resolve_recipient(self.ch(store), "Ramiro")
        assert "solo el nombre" in r["error"]
