"""Aggregate (Count/Sum) fields declared over a ManyToMany manager.

Every other aggregate in the test project hangs off a reverse ForeignKey, so
``AggregateDenorm.m2m_triggers()`` — and with it ``get_related_where()`` and
the ``get_related_*_value()`` pair — was never executed by the suite.  These
tests cover the through-table triggers: the aggregate must react to rows being
added to and removed from the m2m relation itself, not just to the related
objects being created and destroyed.
"""

from django.test import TransactionTestCase
from test_app import models

from denorm import denorms


class TestAggregateM2M(TransactionTestCase):
    def setUp(self):
        denorms.drop_triggers()
        denorms.install_triggers()

    def reload(self, target):
        return models.M2MAggregateTarget.objects.get(pk=target.pk)

    def test_count_and_sum_follow_m2m_membership(self):
        target = models.M2MAggregateTarget.objects.create(name="target")
        self.assertEqual(target.tagger_count, 0)
        self.assertEqual(target.tagger_weight_sum, 0)

        a = models.M2MAggregateTagger.objects.create(name="a", weight=3)
        b = models.M2MAggregateTagger.objects.create(name="b", weight=5)

        # Creating the related objects alone must not move the aggregates;
        # they are not part of the relation yet.
        target = self.reload(target)
        self.assertEqual(target.tagger_count, 0)
        self.assertEqual(target.tagger_weight_sum, 0)

        # Adding through the forward manager (INSERT into the through table).
        a.targets.add(target)
        target = self.reload(target)
        self.assertEqual(target.tagger_count, 1)
        self.assertEqual(target.tagger_weight_sum, 3)

        # ... and through the reverse manager.
        target.taggers.add(b)
        target = self.reload(target)
        self.assertEqual(target.tagger_count, 2)
        self.assertEqual(target.tagger_weight_sum, 8)

        # Removing from the relation (DELETE from the through table).
        target.taggers.remove(a)
        target = self.reload(target)
        self.assertEqual(target.tagger_count, 1)
        self.assertEqual(target.tagger_weight_sum, 5)

        target.taggers.clear()
        target = self.reload(target)
        self.assertEqual(target.tagger_count, 0)
        self.assertEqual(target.tagger_weight_sum, 0)

    def test_sum_follows_updates_of_a_related_row(self):
        target = models.M2MAggregateTarget.objects.create(name="target")
        tagger = models.M2MAggregateTagger.objects.create(name="a", weight=4)
        tagger.targets.add(target)

        target = self.reload(target)
        self.assertEqual(target.tagger_weight_sum, 4)

        tagger.weight = 10
        tagger.save()

        target = self.reload(target)
        self.assertEqual(target.tagger_count, 1)
        self.assertEqual(target.tagger_weight_sum, 10)

    def test_deleting_a_related_row_decrements_once(self):
        target = models.M2MAggregateTarget.objects.create(name="target")
        a = models.M2MAggregateTagger.objects.create(name="a", weight=2)
        b = models.M2MAggregateTagger.objects.create(name="b", weight=7)
        target.taggers.add(a, b)

        target = self.reload(target)
        self.assertEqual(target.tagger_count, 2)
        self.assertEqual(target.tagger_weight_sum, 9)

        # Deleting the object cascades into the through table; the aggregate
        # must be decremented exactly once, not twice.
        a.delete()

        target = self.reload(target)
        self.assertEqual(target.tagger_count, 1)
        self.assertEqual(target.tagger_weight_sum, 7)

    def test_targets_are_tracked_independently(self):
        """The through-table triggers must address a single row.

        A WHERE clause that matched every row of the aggregate's own table
        would still pass the single-target tests above.
        """
        first = models.M2MAggregateTarget.objects.create(name="first")
        second = models.M2MAggregateTarget.objects.create(name="second")
        tagger = models.M2MAggregateTagger.objects.create(name="a", weight=6)

        tagger.targets.add(first)

        self.assertEqual(self.reload(first).tagger_count, 1)
        self.assertEqual(self.reload(first).tagger_weight_sum, 6)
        self.assertEqual(self.reload(second).tagger_count, 0)
        self.assertEqual(self.reload(second).tagger_weight_sum, 0)

        tagger.targets.add(second)
        tagger.targets.remove(first)

        self.assertEqual(self.reload(first).tagger_count, 0)
        self.assertEqual(self.reload(first).tagger_weight_sum, 0)
        self.assertEqual(self.reload(second).tagger_count, 1)
        self.assertEqual(self.reload(second).tagger_weight_sum, 6)
