"""Flush convergence loop (audit spec 2.5): flush_single settles a same-model
denorm chain in ONE transaction instead of N outer passes."""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.contenttypes.models import ContentType


def test_chain_converges_in_one_flush_single(transactional_db, denorm_triggers):
    """full_name -> letterhead chain settles in a single flush_single call
    (today needs two outer passes). save() is called twice WITHIN one
    flush_single (one per chain link)."""
    from test_app.models import Profile

    from denorm import denorms
    from denorm.models import DirtyInstance

    p = Profile.objects.create(first_name="John", last_name="Doe")
    denorms.flush()
    DirtyInstance.objects.all().delete()

    Profile.objects.filter(pk=p.pk).update(last_name="Smith")  # -> 'full_name' marker
    ct = ContentType.objects.get_for_model(Profile)

    saves = []
    orig = Profile.save

    def counting_save(self, *a, **k):
        saves.append(k.get("update_fields"))
        return orig(self, *a, **k)

    with patch.object(Profile, "save", counting_save):
        denorms.flush_single(ct.pk, p.pk, ct)

    assert len(saves) == 2, f"expected 2 saves within one flush_single, got {saves}"
    p.refresh_from_db()
    assert p.full_name == "John Smith"
    assert p.letterhead == "Dear John Smith"
    assert not DirtyInstance.objects.filter(
        content_type=ct, object_id=p.pk
    ).exists(), "object not fully settled by a single flush_single"


def test_scope_discipline_other_object_untouched(transactional_db, denorm_triggers):
    """The convergence loop re-claims ONLY this (ct, oid). Another object's
    markers must be left for the normal flush path."""
    from test_app.models import Profile

    from denorm import denorms
    from denorm.models import DirtyInstance

    a = Profile.objects.create(first_name="A", last_name="One")
    b = Profile.objects.create(first_name="B", last_name="Two")
    denorms.flush()
    DirtyInstance.objects.all().delete()

    ct = ContentType.objects.get_for_model(Profile)
    DirtyInstance.objects.create(content_type=ct, object_id=a.pk, func_name="full_name")
    DirtyInstance.objects.create(content_type=ct, object_id=b.pk, func_name="full_name")

    denorms.flush_single(ct.pk, a.pk, ct)

    assert not DirtyInstance.objects.filter(object_id=a.pk).exists()
    assert DirtyInstance.objects.filter(
        content_type=ct, object_id=b.pk
    ).exists(), "convergence loop consumed another object's marker"


def test_nondeterministic_hits_cap_no_hang(transactional_db, denorm_triggers):
    """A denorm whose value never stabilises must stop at
    DENORM_MAX_CONVERGE_PASSES (no infinite loop), leaving a marker."""
    from test_app.models import Profile

    from denorm import denorms
    from denorm.conf import settings as denorm_settings
    from denorm.models import DirtyInstance

    p = Profile.objects.create(first_name="John", last_name="Doe")
    denorms.flush()
    DirtyInstance.objects.all().delete()
    ct = ContentType.objects.get_for_model(Profile)
    DirtyInstance.objects.create(content_type=ct, object_id=p.pk, func_name="full_name")

    saves = []
    orig = Profile.save

    def churning_save(self, *a, **k):
        # Simulate non-convergence: each save re-inserts a fresh same-object
        # marker, as a non-deterministic denorm's self-trigger would.
        saves.append(1)
        r = orig(self, *a, **k)
        DirtyInstance.objects.create(
            content_type=ct, object_id=self.pk, func_name="full_name"
        )
        return r

    with patch.object(Profile, "save", churning_save):
        denorms.flush_single(ct.pk, p.pk, ct)  # must NOT hang

    cap = denorm_settings.DENORM_MAX_CONVERGE_PASSES
    assert len(saves) == cap, f"expected exactly {cap} saves (the cap), got {len(saves)}"
    assert DirtyInstance.objects.filter(
        content_type=ct, object_id=p.pk
    ).exists(), "cap reached but no marker left for the outer loop"


def test_null_precedence_mid_loop(transactional_db, denorm_triggers):
    """A NULL (whole-object) marker claimed by a later iteration forces a
    full save (no update_fields)."""
    from test_app.models import Profile

    from denorm import denorms
    from denorm.models import DirtyInstance

    p = Profile.objects.create(first_name="John", last_name="Doe")
    denorms.flush()
    DirtyInstance.objects.all().delete()
    ct = ContentType.objects.get_for_model(Profile)
    DirtyInstance.objects.create(content_type=ct, object_id=p.pk, func_name="full_name")

    saw_full_save = []
    orig = Profile.save
    state = {"first": True}

    def hooking_save(self, *a, **k):
        saw_full_save.append("update_fields" not in k)
        r = orig(self, *a, **k)
        if state["first"]:
            state["first"] = False
            # another actor marks the whole object dirty mid-flush
            DirtyInstance.objects.create(content_type=ct, object_id=self.pk)  # NULL
        return r

    with patch.object(Profile, "save", hooking_save):
        denorms.flush_single(ct.pk, p.pk, ct)

    assert any(saw_full_save), "a NULL marker mid-loop must trigger a full save"
    assert not DirtyInstance.objects.filter(content_type=ct, object_id=p.pk).exists()
