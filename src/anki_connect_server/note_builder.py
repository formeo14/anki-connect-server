from dataclasses import dataclass
from typing import cast

from anki.collection import Collection
from anki.decks import DeckId
from anki.models import NotetypeDict
from anki.notes import Note, NoteFieldsCheckResult
from anki.utils import field_checksum

from anki_connect_server.types import JsonObject, JsonValue


@dataclass(frozen=True)
class DuplicateScope:
    allow_duplicate: bool = False
    scope: str | None = None
    deck_name: str | None = None
    check_children: bool = False
    check_all_models: bool = False

    @classmethod
    def from_options(cls, options: JsonValue) -> "DuplicateScope":
        if not isinstance(options, dict):
            return cls()
        scope_options = options.get("duplicateScopeOptions")
        if not isinstance(scope_options, dict):
            scope_options = {}
        return cls(
            allow_duplicate=_bool_option(options, "allowDuplicate"),
            scope=cast(str | None, options.get("duplicateScope")),
            deck_name=cast(str | None, scope_options.get("deckName")),
            check_children=_bool_option(
                scope_options, "checkChildren", "duplicateScopeOptions.checkChildren"
            ),
            check_all_models=_bool_option(
                scope_options, "checkAllModels", "duplicateScopeOptions.checkAllModels"
            ),
        )


def _bool_option(options: JsonObject, key: str, display_name: str | None = None) -> bool:
    value = options.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f'option parameter "{display_name or key}" must be boolean')
    return value


def resolve_deck_id(col: Collection, note: JsonObject) -> DeckId:
    deck_name = note.get("deckName", "")
    deck_id = col.decks.id_for_name(deck_name if isinstance(deck_name, str) else "")
    if deck_id is None:
        raise ValueError(f"deck was not found: {deck_name}")
    return deck_id


def build_note(col: Collection, note: JsonObject, model: NotetypeDict | None) -> Note:
    if model is None:
        raise ValueError(f"model was not found: {note.get('modelName', '')}")
    deck_id = resolve_deck_id(col, note)

    new_note = Note(col, model)
    _fill_fields(new_note, note.get("fields", {}))
    tags = note.get("tags")
    if isinstance(tags, list):
        new_note.tags = [str(t) for t in tags]

    duplicate_scope = DuplicateScope.from_options(note.get("options"))
    state = _fields_check_in_scope(col, new_note, deck_id, duplicate_scope)
    if state == NoteFieldsCheckResult.EMPTY:
        raise ValueError("cannot create note because it is empty")
    if state == NoteFieldsCheckResult.DUPLICATE and not duplicate_scope.allow_duplicate:
        raise ValueError("cannot create note because it is a duplicate")
    if state in (NoteFieldsCheckResult.NORMAL, NoteFieldsCheckResult.DUPLICATE):
        return new_note
    raise ValueError("cannot create note for unknown reason")


def _fill_fields(note: Note, fields: JsonValue) -> None:
    if not isinstance(fields, dict):
        return
    keys_lower = {key.lower(): key for key in note.keys()}  # noqa: SIM118
    for name, value in fields.items():
        matched_name = keys_lower.get(name.lower())
        if matched_name is not None:
            note[matched_name] = str(value) if value is not None else ""


def _fields_check_in_scope(
    col: Collection, note: Note, deck_id: DeckId, scope: DuplicateScope
) -> int:
    if scope.scope != "deck" and not scope.check_all_models:
        return int(note.fields_check())

    first_field = note.fields[0]
    if not first_field.strip():
        return NoteFieldsCheckResult.EMPTY

    deck_ids = _deck_ids_in_scope(col, deck_id, scope)
    if scope.scope == "deck" and deck_ids is None:
        return NoteFieldsCheckResult.NORMAL

    query = "select id from notes where csum=?"
    query_args: list[int] = [field_checksum(first_field)]
    if not scope.check_all_models:
        query += " and mid=?"
        query_args.append(int(note.mid))

    for candidate_note_id in col.db.list(query, *query_args):  # type: ignore[union-attr]
        if deck_ids is None:
            return NoteFieldsCheckResult.DUPLICATE
        card_deck_ids = col.db.list(  # type: ignore[union-attr]
            "select did from cards where nid=?", candidate_note_id
        )
        if any(card_deck_id in deck_ids for card_deck_id in card_deck_ids):
            return NoteFieldsCheckResult.DUPLICATE
    return NoteFieldsCheckResult.NORMAL


def _deck_ids_in_scope(col: Collection, deck_id: DeckId, scope: DuplicateScope) -> set[int] | None:
    if scope.scope != "deck":
        return None
    if scope.deck_name is not None:
        scoped_deck_id = col.decks.id_for_name(scope.deck_name)
        if scoped_deck_id is None:
            return None
        deck_id = scoped_deck_id
    deck_ids = {int(deck_id)}
    if scope.check_children:
        deck_ids.update(int(child_id) for _, child_id in col.decks.children(deck_id))
    return deck_ids
