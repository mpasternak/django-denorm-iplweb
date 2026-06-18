"""Field-feature tests: triggers, cached, abstract, skip, dirty-instance."""

from django.test import TestCase, TransactionTestCase

from test_app import models
import denorm
from denorm import denorms


# Use all but denorms in FailingTriggers models by default
failingdenorms = denorms.get_alldenorms()
denorms.alldenorms = [
    d
    for d in failingdenorms
    if d.model not in (models.FailingTriggersModelA, models.FailingTriggersModelB)
]


class TestTriggers(TestCase):
    def setUp(self):
        denorms.drop_triggers()

    def test_triggers(self):
        """Test potentially failing denorms."""
        # save and restore alldenorms
        # test will fail if it's raising an exception
        alldenorms = denorms.get_alldenorms()
        denorms.alldenorms = failingdenorms
        try:
            denorms.install_triggers()
        finally:
            denorms.alldenorms = alldenorms


class TestCached(TestCase):
    def setUp(self):
        denorms.drop_triggers()
        denorms.install_triggers()

    def tearDown(self):
        models.CachedModelA.objects.all().delete()
        models.CachedModelB.objects.all().delete()

    def test_depends_related(self):
        models.CachedModelB.objects.create(data="Hello")
        b = models.CachedModelB.objects.all()[0]
        self.assertEqual("Hello", b.data)

        models.CachedModelA.objects.create(b=b)
        a = models.CachedModelA.objects.all()[0]

        self.assertEqual("HELLO", a.cached_data["upper"])
        self.assertEqual("hello", a.cached_data["lower"])

        b.data = "World"
        self.assertEqual("HELLO", a.cached_data["upper"])
        self.assertEqual("hello", a.cached_data["lower"])

        b.save()
        a = models.CachedModelA.objects.all()[0]
        self.assertEqual("WORLD", a.cached_data["upper"])
        self.assertEqual("world", a.cached_data["lower"])


class TestAbstract(TestCase):
    def setUp(self):
        denorms.drop_triggers()
        denorms.install_triggers()

    def test_abstract(self):
        d1 = models.RealDenormModel.objects.create(text="onion")
        self.assertEqual("Ham and onion", d1.ham)
        self.assertEqual("Eggs and onion", d1.eggs)


class TestSkip(TransactionTestCase):
    """
    Tests for the skip feature.
    """

    def setUp(self):
        denorms.drop_triggers()
        denorm.models.DirtyInstance.objects.all().delete()
        denorms.install_triggers()

        post = models.SkipPost(text="Here be ponies.")
        post.save()

        self.post = post

    # TODO: Enable and check!
    # Unsure on how to test this behaviour. It results in an endless loop:
    # update -> trigger -> update -> trigger -> ...
    #
    # def test_without_skip(self):
    #    # This results in an infinate loop on SQLite.
    #    comment = SkipCommentWithoutSkip(post=self.post, text='Oh really?')
    #    comment.save()
    #
    #    denorm.flush()

    # TODO: Check if an infinate loop happens and stop it.
    def test_with_skip(self):
        # This should not result in an endless loop.
        comment = models.SkipCommentWithSkip(post=self.post, text="Oh really?")
        comment.save()

        denorm.flush()

    def test_with_only(self):
        # This should not result in an endless loop.
        comment = models.SkipCommentWithOnly(post=self.post, text="Oh really?")
        comment.save()

        denorm.flush()

    def test_meta_skip(self):
        """Test a model with the attribute listed under denorm_always_skip."""
        comment = models.SkipCommentWithAttributeSkip(
            post=self.post, text="Yup, and they have wings!"
        )
        comment.save()

        denorm.flush()


class TestDirtyInstance(TestCase):
    def test___str__(self):
        from test_app.models import CachedModelB

        from denorm.models import DirtyInstance

        x = CachedModelB.objects.create(data="Hello")
        d = DirtyInstance(content_object=x, func_name=None)
        assert str(d).find("DirtyInstance") >= 0
