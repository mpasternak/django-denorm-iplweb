"""Safe eager-mode flush_via_queue drain test."""

from django.test import TransactionTestCase

from test_app import models
import denorm
from denorm import denorms


class FlushViaQueueEagerTestCase(TransactionTestCase):
    """Safe eager-mode drain test: no denorm_queue command, no LISTEN connection."""

    def setUp(self):
        denorms.drop_triggers()
        denorm.models.DirtyInstance.objects.all().delete()
        denorms.install_triggers()

    def tearDown(self):
        denorms.drop_triggers()

    def test_flush_via_queue_drains_eagerly(self):
        """flush_via_queue.apply_async().get() drains DirtyInstance in eager mode.

        Exercises the real Celery task dispatch path (group fan-out, flush_batch,
        flush_single) without using the denorm_queue management command — and
        therefore without a LISTEN connection whose state would be corrupted by
        eager ORM queries. CELERY_TASK_ALWAYS_EAGER=True so .apply_async().get()
        runs all tasks inline.

        The Forum/Post denorm graph is self-dirtying (saving Forum re-marks
        author_names/path/cachekey and related Posts), so a single flush_via_queue
        pass does not drain it. Mirror the pytest live_worker test: re-dispatch in
        a bounded loop until empty or iteration cap.
        """
        from denorm import tasks
        from denorm.models import DirtyInstance

        # Build a simple Forum + Post setup and settle it.
        f1 = models.Forum.objects.create(title="eager-forum")
        models.Post.objects.create(forum=f1, title="eager-post")
        denorms.flush()
        DirtyInstance.objects.all().delete()
        self.assertEqual(DirtyInstance.objects.count(), 0)

        # Dirty deterministically: a new Post triggers Forum denorm markers.
        models.Post.objects.create(forum=f1, title="second-post")
        self.assertGreater(DirtyInstance.objects.count(), 0, "no dirty markers created")

        # Drain via flush_via_queue in eager mode.  The graph is self-dirtying
        # so one call is insufficient; use a bounded re-dispatch loop.
        max_iterations = 20
        for _i in range(max_iterations):
            if not DirtyInstance.objects.exists():
                break
            tasks.flush_via_queue.apply_async().get()

        self.assertEqual(
            DirtyInstance.objects.count(),
            0,
            f"flush_via_queue eager drain did not converge after {max_iterations} passes; "
            f"{DirtyInstance.objects.count()} dirty markers remain.",
        )

        # Verify denorm values are correct after the drain.
        f1.refresh_from_db()
        self.assertEqual(f1.post_count, 2)
