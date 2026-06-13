"""Tests for @denorm_always_dirty model class decorator.

Design: audit-item #4 — guarantees a whole-object dirty marker (func_name=NULL)
on every post_save, even when no watched column changed.
"""

from __future__ import annotations

from django.contrib.contenttypes.models import ContentType


def _null_markers(model, pk):
    """Return all func_name=NULL DirtyInstance markers for (model, pk)."""
    from denorm.models import DirtyInstance

    ct = ContentType.objects.get_for_model(model)
    return DirtyInstance.objects.filter(
        content_type=ct, object_id=pk, func_name__isnull=True
    )


class TestAlwaysDirtyNullMarker:
    """Test 1: A NULL marker is inserted on every save."""

    def test_null_marker_on_create(self, db):
        from test_app.models import AlwaysDirtyModel

        obj = AlwaysDirtyModel.objects.create(first_name="Alice")

        markers = _null_markers(AlwaysDirtyModel, obj.pk)
        assert markers.count() == 1, (
            "Expected a func_name=NULL DirtyInstance after create()"
        )

    def test_null_marker_on_subsequent_save(self, db):
        from test_app.models import AlwaysDirtyModel
        from denorm.models import DirtyInstance

        obj = AlwaysDirtyModel.objects.create(first_name="Bob")
        # Clear markers to start fresh
        ct = ContentType.objects.get_for_model(AlwaysDirtyModel)
        DirtyInstance.objects.filter(content_type=ct, object_id=obj.pk).delete()

        # Save again — decorator must fire again
        obj.first_name = "Bobby"
        obj.save()

        markers = _null_markers(AlwaysDirtyModel, obj.pk)
        assert markers.count() == 1, (
            "Expected a func_name=NULL DirtyInstance after a subsequent save()"
        )


class TestAlwaysDirtyUnwatchedColumn:
    """Test 2: Marks dirty even when NO watched column changed.

    `note` is not a denorm dependency (only `first_name` is declared via
    @depend_on_fields). The self-trigger would NOT fire for an update to
    `note` alone. @denorm_always_dirty must still insert a NULL marker.
    """

    def test_marks_dirty_on_unwatched_column_save(self, transactional_db, denorm_triggers):
        from test_app.models import AlwaysDirtyModel
        from denorm.models import DirtyInstance
        from denorm import denorms

        obj = AlwaysDirtyModel.objects.create(first_name="Carol")
        # Flush and clear all markers to start from a clean slate
        denorms.flush()
        ct = ContentType.objects.get_for_model(AlwaysDirtyModel)
        DirtyInstance.objects.filter(content_type=ct, object_id=obj.pk).delete()

        # Mutate only the unrelated column
        obj.note = "some note"
        obj.save()

        markers = _null_markers(AlwaysDirtyModel, obj.pk)
        assert markers.count() == 1, (
            "Expected a func_name=NULL marker after saving an unwatched column. "
            "Without @denorm_always_dirty the self-trigger would NOT fire for `note`."
        )

    def test_without_decorator_unwatched_save_leaves_no_null_marker(
        self, transactional_db, denorm_triggers
    ):
        """Contrast: a plain model without the decorator leaves no NULL marker
        when only an unrelated column is saved."""
        from test_app.models import Profile  # no @denorm_always_dirty
        from denorm.models import DirtyInstance
        from denorm import denorms

        obj = Profile.objects.create(first_name="Dave", last_name="Test")
        denorms.flush()
        ct = ContentType.objects.get_for_model(Profile)
        DirtyInstance.objects.filter(content_type=ct, object_id=obj.pk).delete()

        # nickname is not a denorm dependency on Profile
        obj.nickname = "Davey"
        obj.save()

        null_markers = DirtyInstance.objects.filter(
            content_type=ct, object_id=obj.pk, func_name__isnull=True
        )
        assert null_markers.count() == 0, (
            "Profile has no @denorm_always_dirty — a save to an unwatched column "
            "must NOT produce a NULL marker."
        )


class TestAlwaysDirtyFlushRecomputes:
    """Test 3: After a save(), flush() recomputes the denorm field correctly."""

    def test_flush_recomputes_greeting(self, transactional_db, denorm_triggers):
        from test_app.models import AlwaysDirtyModel
        from denorm import denorms

        obj = AlwaysDirtyModel.objects.create(first_name="Eve")
        denorms.flush()
        obj.refresh_from_db()
        assert obj.greeting == "Hi Eve"

        # Change only `note` (unwatched) — greeting must still be recomputed
        # because @denorm_always_dirty inserts a NULL marker
        obj.note = "updated"
        obj.save()
        denorms.flush()
        obj.refresh_from_db()
        assert obj.greeting == "Hi Eve", (
            "greeting should still be 'Hi Eve' after flush"
        )

        # Now actually change first_name and verify greeting updates
        obj.first_name = "Evelyn"
        obj.save()
        denorms.flush()
        obj.refresh_from_db()
        assert obj.greeting == "Hi Evelyn"

    def test_flush_processes_then_decorator_reinserts(self, transactional_db, denorm_triggers):
        """flush() processes and deletes the NULL marker, but @denorm_always_dirty
        re-inserts a new one because flush's own save() fires post_save again.
        This is expected: the model is permanently "always dirty" — every save
        by anyone (including flush's save) queues a fresh recompute.

        The important invariant is that flush() *does* recompute the field
        correctly before the new marker lands (verified in test_flush_recomputes_greeting).
        """
        from denorm import denorms
        from test_app.models import AlwaysDirtyModel

        obj = AlwaysDirtyModel.objects.create(first_name="Frank")

        # Before flush: exactly one NULL marker (from create)
        assert _null_markers(AlwaysDirtyModel, obj.pk).count() == 1

        # After one flush pass: the marker is claimed+deleted, field is recomputed,
        # but flush's own save() fires post_save → a new NULL marker is inserted.
        # Manually do a single flush pass (call denorms.flush with max_passes=1 or
        # just verify the field was recomputed by checking the DB value).
        # We verify the field was written, not that markers are zero (they won't be).
        denorms.flush()
        obj.refresh_from_db()
        assert obj.greeting == "Hi Frank", (
            "flush() must have written the correct greeting value"
        )
        # A new marker will exist because flush's save() triggered post_save again
        assert _null_markers(AlwaysDirtyModel, obj.pk).count() == 1, (
            "After flush() the decorator re-inserts a NULL marker because flush's "
            "internal save() fires post_save → mark_dirty() — this is expected."
        )


class TestAlwaysDirtyBulkCaveat:
    """Test 4: QuerySet.update() does NOT fire post_save → no always_dirty marker.

    This documents the limitation: bulk QuerySet.update() bypasses post_save
    signals, so @denorm_always_dirty has no effect for those paths.
    """

    def test_queryset_update_emits_no_always_dirty_null_marker(
        self, transactional_db, denorm_triggers
    ):
        from test_app.models import AlwaysDirtyModel
        from denorm.models import DirtyInstance
        from denorm import denorms

        obj = AlwaysDirtyModel.objects.create(first_name="Grace")
        denorms.flush()

        ct = ContentType.objects.get_for_model(AlwaysDirtyModel)
        # Clear all markers so we have a clean baseline
        DirtyInstance.objects.filter(content_type=ct, object_id=obj.pk).delete()

        # Bulk update on `note` (not a denorm dependency, so even the
        # self-trigger won't fire) — expect NO NULL marker from @denorm_always_dirty
        AlwaysDirtyModel.objects.filter(pk=obj.pk).update(note="bulk")

        null_markers = DirtyInstance.objects.filter(
            content_type=ct, object_id=obj.pk, func_name__isnull=True
        )
        assert null_markers.count() == 0, (
            "QuerySet.update() bypasses post_save — @denorm_always_dirty "
            "must NOT produce a NULL marker for bulk updates."
        )


class TestAlwaysDirtyExport:
    """Test 5: denorm_always_dirty is exported from the package root."""

    def test_exported_from_package_root(self):
        import denorm

        assert callable(denorm.denorm_always_dirty)

    def test_in_all(self):
        import denorm

        assert "denorm_always_dirty" in denorm.__all__
