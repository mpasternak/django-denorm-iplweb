"""Filtered count/sum aggregate tests (Postgres-only guard preserved)."""

from django.db import connection
from django.test import TestCase

from test_app import models
from denorm import denorms


if connection.vendor != "sqlite":

    class TestFilterCount(TestCase):
        """
        Tests for the filtered count feature.
        """

        def setUp(self):
            denorms.drop_triggers()
            denorms.install_triggers()

        def test_filter_count(self):
            master = models.FilterCountModel.objects.create()
            self.assertEqual(master.active_item_count, 0)
            master.items.create(active=True, text="text")
            master.items.create(active=True, text="")
            master = models.FilterCountModel.objects.get(id=master.id)
            self.assertEqual(master.active_item_count, 1, "created active item")
            master.items.create(active=False)
            master = models.FilterCountModel.objects.get(id=master.id)
            self.assertEqual(master.active_item_count, 1, "created inactive item")
            master.items.create(active=True, text="true")
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 2)
            master.items.filter(active=False).delete()
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 2)
            master.items.filter(active=True, text="true")[0].delete()
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 1)
            item = master.items.filter(active=True, text="text")[0]
            item.active = False
            item.save()
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 0)
            item = master.items.filter(active=False, text="text")[0]
            item.active = True
            item.text = ""
            item.save()
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 0)
            item = master.items.filter(active=True, text="")[0]
            item.text = "123"
            item.save()
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 1)

    class TestFilterCountM2M(TestCase):
        """
        Tests for the filtered count feature.
        """

        def setUp(self):
            denorms.drop_triggers()
            denorms.install_triggers()

        def test_filter_count(self):
            master = models.FilterCountModel.objects.create()
            self.assertEqual(master.active_item_count, 0)
            master.items.create(active=True, text="true")
            master = models.FilterCountModel.objects.get(id=master.id)
            self.assertEqual(master.active_item_count, 1, "created active item")
            master.items.create(active=False, text="true")
            master = models.FilterCountModel.objects.get(id=master.id)
            self.assertEqual(master.active_item_count, 1, "created inactive item")
            master.items.create(active=True, text="true")
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 2)
            master.items.filter(active=False).delete()
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 2)
            master.items.filter(active=True)[0].delete()
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 1)
            item = master.items.filter(active=True)[0]
            item.active = False
            item.save()
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 0)
            item = master.items.filter(active=False)[0]
            item.active = True
            item.save()
            master = models.FilterCountModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_count, 1)

    class TestFilterSum(TestCase):
        """
        Tests for the filtered count feature.
        """

        def setUp(self):
            denorms.drop_triggers()
            denorms.install_triggers()

        def test_filter_count(self):
            master = models.FilterSumModel.objects.create()
            self.assertEqual(master.active_item_sum, 0)
            master.counts.create(age=18, active_item_count=8)
            master = models.FilterSumModel.objects.get(id=master.id)
            self.assertEqual(master.active_item_sum, 8)
            master.counts.create(age=16, active_item_count=10)
            master = models.FilterSumModel.objects.get(id=master.id)
            self.assertEqual(master.active_item_sum, 8, "created inactive item")
            master.counts.create(age=19, active_item_count=9)
            master = models.FilterSumModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_sum, 17)
            master.counts.filter(age__lt=18).delete()
            master = models.FilterSumModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_sum, 17)
            master.counts.filter(age=19)[0].delete()
            master = models.FilterSumModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_sum, 8)
            item = master.counts.filter(age=18)[0]
            item.age = 15
            item.save()
            master = models.FilterSumModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_sum, 0)
            item = master.counts.filter(age=15)[0]
            item.age = 18
            item.save()
            master = models.FilterSumModel.objects.get(pk=master.pk)
            self.assertEqual(master.active_item_sum, 8)
