"""Regression tests for the distinct-pair selection in ``denorms.flush()``.

``flush()`` enumerates the dirty markers to process via a
``DISTINCT (content_type_id, object_id)`` query. The query is built to use
the migration 0017 expression index ``COALESCE(object_id, -1)``, which means
NULL ``object_id`` markers are surfaced as the sentinel ``-1`` and must be
mapped back to ``None`` before reaching ``flush_single`` — otherwise a
whole-object marker (``object_id IS NULL``, used for "weird linked foreign
keys") would be flushed against a bogus pk ``-1``.

These tests pin that contract independently of the recompute machinery by
recording the arguments ``flush_single`` receives.
"""

from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TransactionTestCase

from denorm import denorms
from denorm.models import DirtyInstance
from test_app import models


class FlushDistinctPairTestCase(TransactionTestCase):
    def setUp(self):
        DirtyInstance.objects.all().delete()

    def _recorded_flush_calls(self):
        """Run a single flush pass, returning the (ct_id, object_id) pairs
        that would be handed to ``flush_single``."""
        calls = []

        def _record(content_type_id, object_id, *args, **kwargs):
            calls.append((content_type_id, object_id))

        with mock.patch("denorm.denorms.flush_single", side_effect=_record):
            denorms.flush(run_once=True)
        return calls

    def test_null_object_id_preserved_as_none_not_sentinel(self):
        """NULL object_id markers reach flush_single as None, never as -1."""
        ct = ContentType.objects.get_for_model(models.Forum)
        DirtyInstance.objects.create(content_type=ct, object_id=None, func_name="a")
        DirtyInstance.objects.create(content_type=ct, object_id=None, func_name="b")

        calls = self._recorded_flush_calls()

        object_ids = [oid for _ct, oid in calls]
        self.assertIn(None, object_ids)
        self.assertNotIn(-1, object_ids)

    def test_distinct_pairs_deduped_across_func_names(self):
        """Multiple func_names for one (ct, object_id) collapse to a single
        flush_single call, and NULL/real ids coexist as distinct pairs."""
        ct = ContentType.objects.get_for_model(models.Forum)
        # Same (ct, 5) twice -> one pair.
        DirtyInstance.objects.create(content_type=ct, object_id=5, func_name="a")
        DirtyInstance.objects.create(content_type=ct, object_id=5, func_name="b")
        # NULL twice -> one (ct, None) pair.
        DirtyInstance.objects.create(content_type=ct, object_id=None, func_name="a")
        DirtyInstance.objects.create(content_type=ct, object_id=None, func_name="b")
        # A distinct real id.
        DirtyInstance.objects.create(content_type=ct, object_id=7, func_name=None)

        calls = self._recorded_flush_calls()

        self.assertEqual(
            set(calls),
            {(ct.id, 5), (ct.id, None), (ct.id, 7)},
        )
