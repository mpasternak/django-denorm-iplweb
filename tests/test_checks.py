"""Tests for the @depend_on_fields declaration scanner (denorm/checks.py)."""

from __future__ import annotations


def _audit(model, fieldname):
    """Run the scanner for one denorm field of a real model."""
    from denorm.checks import audit_denorm
    from denorm.denorms import get_alldenorms

    for d in get_alldenorms():
        if d.model is model and d.fieldname == fieldname:
            return list(audit_denorm(d))
    raise AssertionError(f"no denorm {model.__name__}.{fieldname}")


class TestScanCallable:
    def test_collects_self_attribute_reads(self):
        from denorm.checks import scan_callable

        def func(self):
            return f"{self.first_name} {self.last_name}"

        reads, called, uncertain = scan_callable(func)
        assert reads == {"first_name", "last_name"}
        assert not called
        assert not uncertain

    def test_detects_dynamic_getattr_as_uncertain(self):
        from denorm.checks import scan_callable

        def func(self):
            return getattr(self, "first" + "_name")

        _, _, uncertain = scan_callable(func)
        assert uncertain

    def test_collects_self_method_calls(self):
        from denorm.checks import scan_callable

        def func(self):
            return self._compute_things()

        _, called, _ = scan_callable(func)
        assert called == {"_compute_things"}


class TestAuditDenorm:
    def test_complete_declaration_is_silent(self, db):
        from test_app.models import Profile

        assert _audit(Profile, "full_name") == []
        assert _audit(Profile, "letterhead") == []

    def test_undeclared_sibling_reads_warn_w001(self, db):
        from test_app.models import UndeclaredProfile

        messages = _audit(UndeclaredProfile, "full_name")
        assert [m.id for m in messages] == ["denorm.W001"]
        assert "first_name" in messages[0].msg
        assert "last_name" in messages[0].msg

    def test_incomplete_declaration_errors_e001(self, db):
        # Synthetic: a declared function that also reads an undeclared
        # column. Built without registering a model, by reusing the real
        # denorm's pieces with a cheating function.
        from test_app.models import Profile

        from denorm.checks import audit_denorm
        from denorm.denorms import get_alldenorms

        d = next(
            x
            for x in get_alldenorms()
            if x.model is Profile and x.fieldname == "full_name"
        )

        def cheating_full_name(self):
            return f"{self.first_name} {self.last_name} {self.nickname}"

        cheating_full_name.__name__ = "full_name"

        class FakeDenorm:
            model = d.model
            fieldname = d.fieldname
            func = staticmethod(cheating_full_name)
            depend = d.depend  # declares only first_name, last_name

        messages = list(audit_denorm(FakeDenorm))
        assert [m.id for m in messages] == ["denorm.E001"]
        assert "nickname" in messages[0].msg

    def test_bad_declared_name_errors_e002(self, db):
        from test_app.models import Profile

        from denorm.checks import audit_denorm
        from denorm.dependencies import DependOnFields
        from denorm.denorms import get_alldenorms

        d = next(
            x
            for x in get_alldenorms()
            if x.model is Profile and x.fieldname == "full_name"
        )

        bad_dep = DependOnFields("no_such_column", func=d.func)
        bad_dep.setup(Profile)

        class FakeDenorm:
            model = d.model
            fieldname = d.fieldname
            func = staticmethod(d.func)
            depend = [bad_dep]

        ids = [m.id for m in audit_denorm(FakeDenorm)]
        assert "denorm.E002" in ids

    def test_dynamic_access_in_declared_func_warns_w002(self, db):
        from test_app.models import Profile

        from denorm.checks import audit_denorm
        from denorm.denorms import get_alldenorms

        d = next(
            x
            for x in get_alldenorms()
            if x.model is Profile and x.fieldname == "full_name"
        )

        def dynamic_full_name(self):
            return getattr(self, "first" + "_name")

        dynamic_full_name.__name__ = "full_name"

        class FakeDenorm:
            model = d.model
            fieldname = d.fieldname
            func = staticmethod(dynamic_full_name)
            depend = d.depend

        ids = [m.id for m in audit_denorm(FakeDenorm)]
        assert "denorm.W002" in ids


    def test_pk_read_is_not_reported_as_sibling(self, db):
        """Reading self.id (or any pk) as an existence guard must not
        produce W001 noise — declaring the pk in @depend_on_fields is
        useless because the pk never changes for a live row."""
        from test_app.models import Member

        messages = _audit(Member, "bookmark_titles")
        ids = [m.id for m in messages]
        assert "denorm.W001" not in ids, (
            "bookmark_titles reads self.id as an existence guard; "
            "the pk never changes and should not be reported as an "
            "undeclared sibling column."
        )


class TestRegisteredCheck:
    def test_registered_check_runs_and_test_models_have_no_errors(self, db):
        from denorm.checks import check_depend_on_fields

        messages = check_depend_on_fields(app_configs=None)
        errors = [m for m in messages if m.id.startswith("denorm.E")]
        assert errors == [], f"test models must stay declaration-honest: {errors}"
        # Member.full_name reads first_name/name without declarations —
        # the nudge warning must fire for it.
        w001_objects = {m.obj for m in messages if m.id == "denorm.W001"}
        assert "Member.full_name" in w001_objects
