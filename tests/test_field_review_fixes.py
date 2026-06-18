"""Regression tests for the denormalized-field audit fixes #3 and #4."""

from __future__ import annotations

from test_app.models import Member


def test_pre_save_caches_none_result_idempotently():
    """Audit #4: ``pre_save`` must be idempotent within one save cycle even
    when the denorm function legitimately returns ``None`` (or any falsy
    value). A nullable denorm field — ``Member.bookmark_titles`` returns
    ``None`` for an unsaved instance — used to defeat the per-save cache
    because the hit was gated on ``cached is not None``, re-evaluating the
    function on the second Django-6.0 pre_save() call.
    """
    field = Member._meta.get_field("bookmark_titles")
    calls = {"n": 0}
    original_func = field.denorm.func

    def counting_func(instance):
        calls["n"] += 1
        return None

    field.denorm.func = counting_func
    try:
        instance = Member(first_name="Ada", name="Lovelace")
        first = field.pre_save(instance, add=True)
        second = field.pre_save(instance, add=True)
    finally:
        field.denorm.func = original_func

    assert first is None
    assert second is None
    assert calls["n"] == 1, "denorm function must be evaluated once per save cycle"


def test_deconstruct_returns_normalized_base_field_values():
    """Audit #3: ``deconstruct`` declares the field reconstructs as a plain
    ``DBField`` (it returns the base field's import path), so the args/kwargs
    it returns must be the normalized values a plain ``DBField`` round-trips
    to — not the dynamic ``DenormDBField`` subclass's raw deconstruct output,
    which could leak denorm-only kwargs into migrations.
    """
    field = Member._meta.get_field("full_name")
    name, path, args, kwargs = field.deconstruct()

    assert path == "django.db.models.CharField"

    base_field_cls = type(field).__mro__[1]
    rebuilt = base_field_cls(*args, **kwargs)
    _, rebuilt_path, rebuilt_args, rebuilt_kwargs = rebuilt.deconstruct()

    # The returned values must be a fixed point of the base field's
    # deconstruct: reconstructing and deconstructing again yields the same
    # tuple. (Before the fix, args/kwargs came from a different source than
    # the path, so this invariant was only accidentally true.)
    assert (path, args, kwargs) == (rebuilt_path, rebuilt_args, rebuilt_kwargs)
