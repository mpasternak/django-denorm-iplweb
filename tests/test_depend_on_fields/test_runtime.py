"""Runtime behaviour: mark_dirty / NULL-marker contract and flush query efficiency."""

from __future__ import annotations

import pytest

from django.contrib.contenttypes.models import ContentType

from ._helpers import _markers


class TestMarkDirtyAndNullContract:
    def test_mark_dirty_emits_null_marker_and_flush_full_saves(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        import denorm
        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        # Corrupt both denorm columns without leaving markers behind.
        Profile.objects.filter(pk=p.pk).update(letterhead="BROKEN")
        DirtyInstance.objects.all().delete()

        denorm.mark_dirty(p)
        marker = _markers(Profile, p.pk).get()
        assert marker.func_name is None

        denorms.flush()
        p.refresh_from_db()
        assert p.full_name == "John Doe"
        assert p.letterhead == "Dear John Doe"

    def test_mark_dirty_is_idempotent(self, transactional_db, denorm_triggers):
        from test_app.models import Profile

        import denorm
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="A", last_name="B")
        DirtyInstance.objects.all().delete()
        denorm.mark_dirty(p)
        denorm.mark_dirty(p)  # unique index + ignore_conflicts: no dupe, no error
        assert _markers(Profile, p.pk).count() == 1

    def test_null_takes_precedence_over_field_markers(
        self, transactional_db, denorm_triggers
    ):
        from unittest.mock import patch

        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        ct = ContentType.objects.get_for_model(Profile)
        DirtyInstance.objects.create(
            content_type=ct, object_id=p.pk, func_name="full_name"
        )
        DirtyInstance.objects.create(content_type=ct, object_id=p.pk)  # NULL

        seen = {}
        orig_save = Profile.save

        def spying_save(self, *args, **kwargs):
            seen["kwargs"] = kwargs
            return orig_save(self, *args, **kwargs)

        with patch.object(Profile, "save", spying_save):
            denorms.flush_single(ct.pk, p.pk, ct)

        assert "update_fields" not in seen["kwargs"], (
            "A func_name=NULL marker must force a FULL save (whole-object "
            "recompute), taking precedence over coexisting field markers."
        )

    def test_unknown_func_name_falls_back_to_full_save(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="John", last_name="Doe")
        denorms.flush()
        DirtyInstance.objects.all().delete()

        # Corrupt BOTH denorm columns directly so a targeted save would only
        # heal the one it knows about — a full save must heal everything.
        Profile.objects.filter(pk=p.pk).update(letterhead="STALE")
        DirtyInstance.objects.all().delete()

        ct = ContentType.objects.get_for_model(Profile)
        # e.g. a marker from a field that was removed in a later deploy
        DirtyInstance.objects.create(
            content_type=ct, object_id=p.pk, func_name="removed_in_v2"
        )

        denorms.flush_single(ct.pk, p.pk, ct)  # must not raise
        assert not DirtyInstance.objects.exists()

        # Full save must have recomputed BOTH fields, healing the corruption.
        p.refresh_from_db()
        assert p.full_name == "John Doe", (
            "Unknown func_name must trigger a full save that recomputes full_name."
        )
        assert p.letterhead == "Dear John Doe", (
            "Unknown func_name must trigger a full save that heals ALL denorm "
            "columns, including letterhead which was corrupted to 'STALE'."
        )

    def test_mark_dirty_rejects_unsaved_instances(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import Profile

        import denorm

        with pytest.raises(ValueError):
            denorm.mark_dirty(Profile(first_name="X", last_name="Y"))


class TestFlushQueryEfficiency:
    def test_flush_single_uses_contenttype_cache(self, transactional_db, denorm_triggers):
        """ContentType.objects.get(pk=...) bypasses Django's ContentType
        cache — a 100k-marker flush issues 100k identical queries.
        get_for_id() hits the per-process cache (spec 2.3)."""
        from unittest.mock import patch

        from test_app.models import Profile

        from denorm import denorms
        from denorm.models import DirtyInstance

        p = Profile.objects.create(first_name="A", last_name="B")
        denorms.flush()
        DirtyInstance.objects.all().delete()
        ct = ContentType.objects.get_for_model(Profile)
        DirtyInstance.objects.create(
            content_type=ct, object_id=p.pk, func_name="full_name"
        )

        with patch.object(
            ContentType.objects, "get_for_id", wraps=ContentType.objects.get_for_id
        ) as spy:
            denorms.flush_single(ct.pk, p.pk)  # no content_type kwarg

        spy.assert_called_once_with(ct.pk)

    def test_marker_claim_matches_expression_index(self, db):
        """The 0017 unique index keys on COALESCE(object_id, -1); a filter
        on raw object_id can only use the content_type prefix, making a
        large single-model flush O(N^2). The claim query must emit the
        same COALESCE expression so both index columns are usable
        (spec 2.6)."""
        from denorm.denorms import _markers_for

        sql = str(_markers_for(42, 7).query)
        assert 'COALESCE("denorm_dirtyinstance"."object_id", -1)' in sql
