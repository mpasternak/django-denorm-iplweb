from django.apps import AppConfig
from django.conf import settings
from django.db.models.signals import post_migrate


def denorm_install_triggers_after_migrate(using, **kwargs):
    from denorm import denorms

    denorms.install_triggers(using=using)


class DenormAppConfig(AppConfig):
    name = "denorm"

    def ready(self):
        if getattr(settings, "DENORM_INSTALL_TRIGGERS_AFTER_MIGRATE", True):
            post_migrate.connect(denorm_install_triggers_after_migrate, sender=self)
