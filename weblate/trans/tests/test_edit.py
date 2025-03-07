# Copyright © Michal Čihař <michal@weblate.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Test for translation views."""

import time
from unittest import SkipTest

from django.urls import reverse

from weblate.addons.resx import ResxUpdateAddon
from weblate.checks.models import Check
from weblate.trans.models import Change, Component, Unit
from weblate.trans.tests.test_views import ViewTestCase
from weblate.trans.util import join_plural
from weblate.utils.hash import hash_to_checksum
from weblate.utils.state import STATE_FUZZY, STATE_READONLY, STATE_TRANSLATED


class EditTest(ViewTestCase):
    """Test for manipulating translation."""

    has_plurals = True
    source = "Hello, world!\n"
    target = "Nazdar svete!\n"
    second_target = "Ahoj svete!\n"
    already_translated = 0
    needs_bilingual_context = False
    new_source_string = "Source string" * 100000

    def setUp(self):
        super().setUp()
        self.translate_url = reverse("translate", kwargs=self.kw_translation)

    def test_edit(self):
        response = self.edit_unit(self.source, self.target)
        # We should get to second message
        self.assert_redirects_offset(response, self.translate_url, 2)
        unit = self.get_unit(source=self.source)
        self.assertEqual(unit.target, self.target)
        self.assertEqual(len(unit.all_checks), 0)
        self.assertEqual(unit.state, STATE_TRANSLATED)
        self.assert_backend(self.already_translated + 1)

        # Test that second edit with no change does not break anything
        response = self.edit_unit(self.source, self.target)
        # We should get to second message
        self.assert_redirects_offset(response, self.translate_url, 2)
        unit = self.get_unit(source=self.source)
        self.assertEqual(unit.target, self.target)
        self.assertEqual(len(unit.all_checks), 0)
        self.assertEqual(unit.state, STATE_TRANSLATED)
        self.assert_backend(self.already_translated + 1)

        # Test that third edit still works
        response = self.edit_unit(self.source, self.second_target)
        # We should get to second message
        self.assert_redirects_offset(response, self.translate_url, 2)
        unit = self.get_unit(source=self.source)
        self.assertEqual(unit.target, self.second_target)
        self.assertEqual(len(unit.all_checks), 0)
        self.assertEqual(unit.state, STATE_TRANSLATED)
        self.assert_backend(self.already_translated + 1)

    def test_plurals(self):
        """Test plural editing."""
        if not self.has_plurals:
            return

        response = self.edit_unit(
            "Orangutan",
            "Opice má %d banán.\n",
            target_1="Opice má %d banány.\n",
            target_2="Opice má %d banánů.\n",
        )
        # We should get to next message
        self.assert_redirects_offset(response, self.translate_url, 3)
        # Check translations
        unit = self.get_unit("Orangutan")
        plurals = unit.get_target_plurals()
        self.assertEqual(len(plurals), 3)
        self.assertEqual(plurals[0], "Opice má %d banán.\n")
        self.assertEqual(plurals[1], "Opice má %d banány.\n")
        self.assertEqual(plurals[2], "Opice má %d banánů.\n")

    def test_fuzzy(self):
        """Test for fuzzy flag handling."""
        unit = self.get_unit(source=self.source)
        self.assertNotEqual(unit.state, STATE_FUZZY)

        self.edit_unit(self.source, self.target, fuzzy="yes", review="10")
        unit = self.get_unit(source=self.source)
        self.assertEqual(unit.state, STATE_FUZZY)
        self.assertEqual(unit.target, self.target)
        self.assertFalse(unit.has_failing_check)

        self.edit_unit(self.source, self.target)
        unit = self.get_unit(source=self.source)
        self.assertEqual(unit.state, STATE_TRANSLATED)
        self.assertEqual(unit.target, self.target)
        self.assertFalse(unit.has_failing_check)

        self.edit_unit(self.source, self.target, fuzzy="yes")
        unit = self.get_unit(source=self.source)
        self.assertEqual(unit.state, STATE_FUZZY)
        self.assertEqual(unit.target, self.target)
        self.assertFalse(unit.has_failing_check)

        # Should not have was translated check
        self.edit_unit(self.source, "")
        unit = self.get_unit(source=self.source)
        self.assertFalse(unit.has_failing_check)

    def add_unit(self, key, force_source: bool = False):
        if force_source or self.component.has_template():
            args = {"context": key, "source_0": self.new_source_string}
            language = "en"
        else:
            args = {"source_0": key, "target_0": "Translation string"}
            if self.needs_bilingual_context:
                args["context"] = key * 2
            language = "cs"
        return self.client.post(
            reverse(
                "new-unit",
                kwargs={
                    "path": [self.component.project.slug, self.component.slug, language]
                },
            ),
            args,
            follow=True,
        )

    def test_new_unit(self):
        # No permissions
        response = self.add_unit("key")
        self.assertEqual(response.status_code, 403)

        self.make_manager()

        # No adding
        self.component.manage_units = False
        self.component.save()
        response = self.add_unit("key")
        self.assertEqual(response.status_code, 403)

        # Adding allowed (if format supports that)
        self.component.manage_units = True
        self.component.save()
        response = self.add_unit("key")
        if not self.component.file_format_cls.can_add_unit:
            self.assertEqual(response.status_code, 403)
            return
        self.assertContains(response, "New string has been added")

        # Duplicate string
        response = self.add_unit("key")
        self.assertContains(response, "This string seems to already exist.")

        # Invalid params
        response = self.add_unit("")
        self.assertContains(response, "Error in parameter ")

        # Adding on source in bilingual
        if (
            not self.component.has_template()
            and self.component.file_format_cls.can_add_unit
        ):
            start = Unit.objects.count()
            response = self.add_unit("Test string", force_source=True)
            self.assertContains(response, "New string has been added")
            self.assertEqual(
                start + self.component.translation_set.count(),
                Unit.objects.count(),
            )

        # Make sure writing out pending units works
        self.component.commit_pending("test", None)


class EditValidationTest(ViewTestCase):
    def edit(self, **kwargs):
        """Editing with no specific params."""
        unit = self.get_unit()
        params = {"checksum": unit.checksum}
        params.update(kwargs)
        return self.client.post(
            unit.translation.get_translate_url(), params, follow=True
        )

    def test_edit_invalid(self):
        """Editing with invalid params."""
        response = self.edit()
        self.assertContains(response, "This field is required.")

    def test_suggest_invalid(self):
        """Suggesting with invalid params."""
        response = self.edit(suggest="1")
        self.assertContains(response, "This field is required.")

    def test_merge(self):
        """Merging with invalid parameter."""
        unit = self.get_unit()
        response = self.client.post(
            unit.translation.get_translate_url() + "?checksum=" + unit.checksum,
            {"merge": "invalid"},
            follow=True,
        )
        self.assertContains(response, "Invalid merge request!")

    def test_merge_lang(self):
        """Merging across languages."""
        unit = self.get_unit()
        trans = self.component.translation_set.exclude(language_code="cs")[0]
        other = trans.unit_set.get(source=unit.source, context=unit.context)
        response = self.client.post(
            unit.translation.get_translate_url() + "?checksum=" + unit.checksum,
            {"merge": other.pk},
            follow=True,
        )
        self.assertContains(response, "Invalid merge request!")

    def test_revert(self):
        unit = self.get_unit()
        # Try the merge
        response = self.client.get(
            unit.translation.get_translate_url(),
            {"checksum": unit.checksum, "revert": "invalid"},
            follow=True,
        )
        self.assertContains(response, "Invalid revert request!")
        # Try the merge
        response = self.client.get(
            unit.translation.get_translate_url(),
            {"checksum": unit.checksum, "revert": -1},
            follow=True,
        )
        self.assertContains(response, "Invalid revert request!")


class EditResourceTest(EditTest):
    has_plurals = False

    def create_component(self):
        return self.create_android()

    def test_new_unit_translate(self, commit_translation: bool = False):
        """Test for translating newly added string, issue #6890."""
        self.make_manager()
        self.component.manage_units = True
        self.component.save()

        # Add new string
        response = self.add_unit("key")
        self.assertContains(response, "New string has been added")
        self.assertEqual(Unit.objects.filter(pending=True).count(), 1)
        self.assertEqual(Unit.objects.filter(context="key").count(), 2)

        # Edit unit
        self.edit_unit(source=self.new_source_string, target="Překlad")
        self.assertEqual(Unit.objects.filter(pending=True).count(), 2)

        # Commit to the file
        if commit_translation:
            translation = self.get_translation()
            translation.commit_pending("test", None)
        else:
            self.component.commit_pending("test", None)
        self.assertEqual(Unit.objects.filter(pending=True).count(), 0)
        self.assertEqual(Unit.objects.filter(context="key").count(), 2)
        self.assertEqual(
            Unit.objects.filter(context="key", state=STATE_TRANSLATED).count(), 2
        )
        self.component.create_translations(force=True)
        self.assertEqual(
            Unit.objects.filter(context="key", state=STATE_TRANSLATED).count(), 2
        )

    def test_new_unit_translate_commit_translation(self, commit_translation=False):
        self.test_new_unit_translate(commit_translation=True)


class EditResxTest(EditTest):
    has_plurals = False

    def create_component(self):
        component = self.create_resx()
        ResxUpdateAddon.create(component)
        return component


class EditLanguageTest(EditTest):
    """Language wide editing tests."""

    def setUp(self):
        super().setUp()
        self.translate_url = reverse(
            "translate",
            kwargs={"path": [self.project.slug, "-", "cs"]},
        )

    def edit_unit(self, source, target, language="cs", **kwargs):
        """Do edit single unit using web interface."""
        unit = self.get_unit(source, language)
        params = {
            "checksum": unit.checksum,
            "contentsum": hash_to_checksum(unit.content_hash),
            "translationsum": hash_to_checksum(unit.get_target_hash()),
            "target_0": target,
            "review": "20",
        }
        params.update(kwargs)
        return self.client.post(self.translate_url, params)


class EditResourceSourceTest(ViewTestCase):
    """Source strings (template) editing."""

    has_plurals = False

    def test_edit(self):
        translate_url = reverse(
            "translate",
            kwargs={"path": self.component.source_translation.get_url_path()},
        )

        response = self.edit_unit("Hello, world!\n", "Nazdar svete!\n", "en")
        # We should get to second message
        self.assert_redirects_offset(response, translate_url, 2)
        unit = self.get_unit("Nazdar svete!\n", "en")
        self.assertEqual(unit.target, "Nazdar svete!\n")
        self.assertEqual(len(unit.all_checks), 0)
        self.assertEqual(unit.state, STATE_TRANSLATED)
        self.assert_backend(4, "en")

    def test_edit_revert(self):
        translation = self.get_translation()
        # Edit translation
        self.edit_unit("Hello, world!\n", "Nazdar svete!\n", "cs")

        unit = translation.unit_set.get(context="hello")
        self.assertEqual(unit.state, STATE_TRANSLATED)

        # Edit source
        self.edit_unit("Hello, world!\n", "Hello, universe!\n", "en")

        unit = translation.unit_set.get(context="hello")
        self.assertEqual(unit.state, STATE_FUZZY)

        # Revert source
        self.edit_unit("Hello, universe!\n", "Hello, world!\n", "en")

        unit = translation.unit_set.get(context="hello")
        self.assertEqual(unit.state, STATE_TRANSLATED)

    def test_needs_edit(self):
        translation = self.get_translation()

        # Edit translation
        self.edit_unit("Hello, world!\n", "Nazdar svete!\n", "cs")

        # Change state
        self.edit_unit("Hello, world!\n", "Hello, world!\n", "en", fuzzy="yes")
        unit = translation.unit_set.get(context="hello")
        self.assertEqual(unit.state, STATE_READONLY)

        # Change state and source
        self.edit_unit("Hello, world!\n", "Hello, universe!\n", "en", fuzzy="yes")
        unit = translation.unit_set.get(context="hello")
        self.assertEqual(unit.state, STATE_READONLY)

        # Change state and source
        self.edit_unit("Hello, universe!\n", "Hello, universe!\n", "en")
        unit = translation.unit_set.get(context="hello")
        self.assertEqual(unit.state, STATE_FUZZY)

        # Revert source
        self.edit_unit("Hello, universe!\n", "Hello, world!\n", "en")
        unit = translation.unit_set.get(context="hello")
        self.assertEqual(unit.state, STATE_TRANSLATED)

    def create_component(self):
        return self.create_android()


class EditBranchTest(EditTest):
    def create_component(self):
        return self.create_po_branch()


class EditMercurialTest(EditTest):
    def create_component(self):
        return self.create_po_mercurial()


class EditPoMonoTest(EditTest):
    def create_component(self):
        return self.create_po_mono()

    def test_remove_unit(self):
        self.assertEqual(self.component.stats.all, 16)
        unit_count = Unit.objects.count()
        unit = self.get_unit()
        # Deleting translation unit
        response = self.client.post(reverse("delete-unit", kwargs={"unit_id": unit.pk}))
        self.assertEqual(response.status_code, 403)
        # Lack of permissions
        response = self.client.post(
            reverse("delete-unit", kwargs={"unit_id": unit.source_unit.pk})
        )
        self.assertEqual(response.status_code, 403)
        # Make superuser
        self.user.is_superuser = True
        self.user.save()
        # Deleting translation unit
        response = self.client.post(reverse("delete-unit", kwargs={"unit_id": unit.pk}))
        self.assertEqual(response.status_code, 403)
        # Actual removal
        response = self.client.post(
            reverse("delete-unit", kwargs={"unit_id": unit.source_unit.pk})
        )
        self.assertEqual(response.status_code, 302)
        component = Component.objects.get(pk=self.component.pk)
        self.assertEqual(component.stats.all, 12)
        self.assertEqual(unit_count - 4, Unit.objects.count())


class EditIphoneTest(EditTest):
    has_plurals = False

    def create_component(self):
        return self.create_iphone()

    def test_new_unit(self):
        # Most likely the test is wrong here it is using monolingual format as bilingual
        # and duplicates source into context
        raise SkipTest("Not supported")


class EditJSONTest(EditTest):
    has_plurals = False

    def create_component(self):
        return self.create_json()


class EditJoomlaTest(EditTest):
    has_plurals = False

    def create_component(self):
        return self.create_joomla()


class EditRubyYAMLTest(EditTest):
    has_plurals = False

    def create_component(self):
        return self.create_ruby_yaml()


class EditDTDTest(EditTest):
    has_plurals = False

    def create_component(self):
        return self.create_dtd()

    def test_new_unit(self):
        # Most likely there is a bug in the format and adding is broken
        raise SkipTest("Not supported")


class EditJSONMonoTest(EditTest):
    has_plurals = False

    def create_component(self):
        return self.create_json_mono()

    def test_new_unit_validation(self):
        self.make_manager()
        self.component.manage_units = True
        self.component.file_format = "json-nested"
        self.component.save()
        response = self.add_unit("key")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New string has been added")


class EditJavaTest(EditTest):
    has_plurals = False
    already_translated = 1

    def create_component(self):
        return self.create_java()

    def test_untranslate(self):
        translation = self.get_translation()

        # Edit translation
        self.edit_unit("Hello, world!\n", "Nazdar svete!\n", "cs")
        self.component.commit_pending("test", None)
        self.assertEqual(translation.unit_set.filter(state=STATE_TRANSLATED).count(), 1)

        # Untranslate
        self.edit_unit("Hello, world!\n", "", "cs")
        self.assertEqual(translation.unit_set.filter(state=STATE_TRANSLATED).count(), 0)
        self.component.commit_pending("test", None)
        self.assertEqual(translation.unit_set.filter(state=STATE_TRANSLATED).count(), 0)


class EditAppStoreTest(EditTest):
    has_plurals = False
    source = "Weblate - continuous localization"
    target = "Weblate - průběžná lokalizace"
    second_target = "Weblate - průběžný překlad"
    already_translated = 2

    def create_component(self):
        return self.create_appstore()


class EditXliffComplexTest(EditTest):
    has_plurals = False
    needs_bilingual_context = True

    def create_component(self):
        return self.create_xliff("complex")

    def test_invalid_xml(self):
        self.edit_unit("Hello, world!\n", "Nazdar & svete!\n")
        self.assert_backend(1)

    def test_new_unit(self):
        # The group handling is broken, see
        # https://github.com/translate/translate/issues/4186
        raise SkipTest("Not supported")


class EditXliffResnameTest(EditTest):
    has_plurals = False
    needs_bilingual_context = True

    def create_component(self):
        return self.create_xliff("only-resname")


class EditXliffTest(EditTest):
    has_plurals = False
    needs_bilingual_context = True

    def create_component(self):
        return self.create_xliff()


class EditXliffMonoTest(EditTest):
    has_plurals = False

    def create_component(self):
        return self.create_xliff_mono()

    def test_new_unit(self):
        # The group handling is broken, see
        # https://github.com/translate/translate/issues/4186
        raise SkipTest("Not supported")


class EditLinkTest(EditTest):
    def create_component(self):
        return self.create_link()


class EditTSTest(EditTest):
    def create_component(self):
        return self.create_ts()


class EditTSMonoTest(EditTest):
    has_plurals = False

    def create_component(self):
        return self.create_ts_mono()


class ZenViewTest(ViewTestCase):
    def test_zen(self):
        response = self.client.get(reverse("zen", kwargs=self.kw_translation))
        self.assertContains(response, "Thank you for using Weblate.")
        self.assertContains(response, "Orangutan has %d bananas")
        self.assertContains(response, "The translation has come to an end.")

    def test_zen_invalid(self):
        response = self.client.get(
            reverse("zen", kwargs=self.kw_translation),
            {"q": "has:nonexisting"},
            follow=True,
        )
        self.assertContains(response, "Unsupported has lookup")

    def test_load_zen(self):
        response = self.client.get(reverse("load_zen", kwargs=self.kw_translation))
        self.assertContains(response, "Thank you for using Weblate.")
        self.assertContains(response, "Orangutan has %d bananas")
        self.assertContains(response, "The translation has come to an end.")

    def test_load_zen_offset(self):
        response = self.client.get(
            reverse("load_zen", kwargs=self.kw_translation),
            {"offset": "2"},
        )
        self.assertNotContains(response, "Hello, world")
        self.assertContains(response, "Orangutan has %d bananas")
        response = self.client.get(
            reverse("load_zen", kwargs=self.kw_translation),
            {"offset": "bug"},
        )
        self.assertContains(response, "Hello, world")

    def test_save_zen(self):
        unit = self.get_unit()
        params = {
            "checksum": unit.checksum,
            "contentsum": hash_to_checksum(unit.content_hash),
            "translationsum": hash_to_checksum(unit.get_target_hash()),
            "target_0": "Zen translation",
            "review": "20",
        }
        response = self.client.post(
            reverse("save_zen", kwargs=self.kw_translation),
            params,
        )
        self.assertContains(
            response,
            "Following fixups were applied to translation: "
            "Trailing and leading whitespace",
        )

    def test_save_zen_lock(self):
        self.component.locked = True
        self.component.save()
        unit = self.get_unit()
        params = {
            "checksum": unit.checksum,
            "contentsum": hash_to_checksum(unit.content_hash),
            "translationsum": hash_to_checksum(unit.get_target_hash()),
            "target_0": "Zen translation",
            "review": "20",
        }
        response = self.client.post(
            reverse("save_zen", kwargs=self.kw_translation),
            params,
        )
        self.assertContains(
            response, "Insufficient privileges for saving translations."
        )

    def test_browse(self):
        response = self.client.get(reverse("browse", kwargs=self.kw_translation))
        self.assertContains(response, "Thank you for using Weblate.")
        self.assertContains(
            response,
            'Orangutan has <span class="hlcheck" data-value="%d"><span class="highlight-number"></span>%d</span> banana.',
        )


class EditComplexTest(ViewTestCase):
    """Test for complex manipulating translation."""

    def setUp(self):
        super().setUp()
        self.translation = self.get_translation()
        self.translate_url = reverse("translate", kwargs=self.kw_translation)

    def test_merge(self):
        # Translate unit to have something to start with
        response = self.edit_unit("Hello, world!\n", "Nazdar svete!\n")
        unit = self.get_unit()
        # Try the merge
        response = self.client.post(
            self.translate_url + "?checksum=" + unit.checksum, {"merge": unit.id}
        )
        self.assert_backend(1)
        # We should stay on same message
        self.assert_redirects_offset(response, self.translate_url, unit.position + 1)

        # Test error handling
        unit2 = self.translation.unit_set.get(source="Thank you for using Weblate.")
        response = self.client.post(
            self.translate_url + "?checksum=" + unit.checksum, {"merge": unit2.id}
        )
        self.assertContains(response, "Invalid merge request!")

    def test_merge_inconsistent(self):
        # Translate unit to have something to start with
        self.edit_unit("Hello, world!\n", "Nazdar svete!\n")
        units = Unit.objects.filter(
            translation__language__code="cs", source="Hello, world!\n"
        )
        self.assertEqual(
            set(units.values_list("target", flat=True)), {"Nazdar svete!\n"}
        )
        self.create_link_existing()
        self.assertEqual(
            set(units.values_list("target", flat=True)), {"Nazdar svete!\n", ""}
        )
        unit = self.get_unit()
        self.assertEqual(unit.all_checks_names, {"inconsistent"})
        self.client.post(
            self.translate_url + "?checksum=" + unit.checksum, {"merge": unit.id}
        )
        self.assertEqual(
            set(units.values_list("target", flat=True)), {"Nazdar svete!\n"}
        )
        unit = self.get_unit()
        self.assertEqual(unit.all_checks_names, set())

    def test_edit_propagated(self):
        units = Unit.objects.filter(
            translation__language__code="cs", source="Thank you for using Weblate."
        )
        self.create_link_existing()
        self.assertEqual(set(units.values_list("target", flat=True)), {""})
        self.edit_unit("Thank you for using Weblate.", "Díky za použití Weblate")
        self.assertEqual(
            set(units.values_list("target", flat=True)), {"Díky za použití Weblate"}
        )
        self.assertEqual(
            [unit.all_checks_names for unit in units.iterator()],
            [{"end_stop"}, {"end_stop"}],
        )
        self.edit_unit("Thank you for using Weblate.", "Díky za použití Weblate.")
        self.assertEqual(
            set(units.values_list("target", flat=True)), {"Díky za použití Weblate."}
        )
        self.assertEqual(
            [unit.all_checks_names for unit in units.iterator()], [set(), set()]
        )

    def test_revert(self):
        source = "Hello, world!\n"
        target = "Nazdar svete!\n"
        target_2 = "Hei maailma!\n"
        self.edit_unit(source, target)
        # Ensure other edit gets different timestamp
        time.sleep(1)
        self.edit_unit(source, target_2)
        unit = self.get_unit()
        changes = Change.objects.content().filter(unit=unit).order()
        self.assertEqual(changes[1].target, target)
        self.assertEqual(changes[0].target, target_2)
        self.assert_backend(1)
        # revert it
        self.client.get(
            self.translate_url, {"checksum": unit.checksum, "revert": changes[1].id}
        )
        unit = self.get_unit()
        self.assertEqual(unit.target, target_2)
        # check that we cannot revert to string from another translation
        self.edit_unit("Thank you for using Weblate.", "Kiitoksia Weblaten kaytosta.")
        unit2 = self.get_unit(source="Thank you for using Weblate.")
        change = unit2.change_set.order()[0]
        response = self.client.get(
            self.translate_url, {"checksum": unit.checksum, "revert": change.id}
        )
        self.assertContains(response, "Invalid revert request!")
        self.assert_backend(2)

    def test_revert_plural(self):
        source = "Orangutan has %d banana.\n"
        target = [
            "Opice má %d banán.\n",
            "Opice má %d banány.\n",
            "Opice má %d banánů.\n",
        ]
        target_2 = [
            "Orangutan má %d banán.\n",
            "Orangutan má %d banány.\n",
            "Orangutan má %d banánů.\n",
        ]
        self.edit_unit(source, target[0], target_1=target[1], target_2=target[2])
        # Ensure other edit gets different timestamp
        time.sleep(1)
        self.edit_unit(source, target_2[0], target_1=target_2[1], target_2=target_2[2])
        unit = self.get_unit(source)
        changes = Change.objects.content().filter(unit=unit).order()
        self.assertEqual(changes[1].target, join_plural(target))
        self.assertEqual(changes[0].target, join_plural(target_2))
        self.assert_backend(1)
        # revert it
        self.client.get(
            self.translate_url, {"checksum": unit.checksum, "revert": changes[0].id}
        )
        unit = self.get_unit(source)
        self.assertEqual(unit.get_target_plurals(), target)

    def test_edit_fixup(self):
        # Save with failing check
        response = self.edit_unit("Hello, world!\n", "Nazdar svete!")
        # We should get to second message
        self.assert_redirects_offset(response, self.translate_url, 2)
        unit = self.get_unit()
        self.assertEqual(unit.target, "Nazdar svete!\n")
        self.assertFalse(unit.has_failing_check)
        self.assertEqual(len(unit.all_checks), 0)
        self.assertEqual(len(unit.active_checks), 0)
        self.assertEqual(unit.translation.stats.allchecks, 0)
        self.assert_backend(1)

    def test_edit_check(self):
        # Save with failing check
        response = self.edit_unit("Hello, world!\n", "Hello, world!\n")
        # We should stay on current message
        self.assert_redirects_offset(response, self.translate_url, 1)
        unit = self.get_unit()
        self.assertEqual(unit.target, "Hello, world!\n")
        self.assertTrue(unit.has_failing_check)
        self.assertEqual(unit.state, STATE_TRANSLATED)
        self.assertEqual(len(unit.all_checks), 1)
        self.assertEqual(len(unit.active_checks), 1)
        self.assertEqual(unit.translation.stats.allchecks, 1)

        # Ignore check
        check_id = unit.active_checks[0].id
        response = self.client.post(
            reverse("js-ignore-check", kwargs={"check_id": check_id})
        )
        self.assertContains(response, "ok")

        # Should have one less failing check
        unit = self.get_unit()
        self.assertFalse(unit.has_failing_check)
        self.assertEqual(len(unit.all_checks), 1)
        self.assertEqual(len(unit.active_checks), 0)
        self.assertEqual(unit.translation.stats.allchecks, 0)

        # Ignore check for all languages
        ignore_flag = Check.objects.get(pk=int(check_id)).check_obj.ignore_string
        ignore_url = reverse("js-ignore-check-source", kwargs={"check_id": check_id})
        response = self.client.post(ignore_url)
        self.assertEqual(response.status_code, 403)
        self.user.is_superuser = True
        self.user.save()
        response = self.client.post(ignore_url)
        self.assertEqual(response.headers["Content-Type"], "application/json")

        # Should have one less check
        unit = self.get_unit()
        self.assertJSONEqual(
            response.content.decode("utf-8"),
            {
                "extra_flags": ignore_flag,
                "all_flags": unit.all_flags.format(),
                "ignore_check": ignore_flag,
            },
        )
        self.assertFalse(unit.has_failing_check)
        self.assertEqual(len(unit.all_checks), 0)
        self.assertEqual(len(unit.active_checks), 0)
        self.assertEqual(unit.translation.stats.allchecks, 0)

        # Save with no failing checks
        response = self.edit_unit("Hello, world!\n", "Nazdar svete!\n")
        # We should stay on current message
        self.assert_redirects_offset(response, self.translate_url, 2)
        unit = self.get_unit()
        self.assertEqual(unit.target, "Nazdar svete!\n")
        self.assertFalse(unit.has_failing_check)
        self.assertEqual(len(unit.all_checks), 0)
        self.assertEqual(unit.translation.stats.allchecks, 0)
        self.assert_backend(1)

    def test_enforced_check(self):
        # Enforce same check
        self.component.enforced_checks = ["same"]
        self.component.save(update_fields=["enforced_checks"])
        # Save with failing check
        response = self.edit_unit("Hello, world!\n", "Hello, world!\n")
        # We should stay on current message
        self.assert_redirects_offset(response, self.translate_url, 1)
        unit = self.get_unit()
        self.assertEqual(unit.target, "Hello, world!\n")
        self.assertEqual(unit.state, STATE_FUZZY)
        self.assertTrue(unit.has_failing_check)
        self.assertEqual(len(unit.all_checks), 1)
        self.assertEqual(len(unit.active_checks), 1)
        self.assertEqual(unit.translation.stats.allchecks, 1)

    def test_commit_push(self):
        response = self.edit_unit("Hello, world!\n", "Nazdar svete!\n")
        # We should get to second message
        self.assert_redirects_offset(response, self.translate_url, 2)
        self.assertTrue(self.translation.needs_commit())
        self.assertTrue(self.component.needs_commit())
        self.assertTrue(self.component.project.needs_commit())

        self.translation.commit_pending("test", self.user)

        self.assertFalse(self.translation.needs_commit())
        self.assertFalse(self.component.needs_commit())
        self.assertFalse(self.component.project.needs_commit())

        self.assertTrue(self.translation.repo_needs_push())
        self.assertTrue(self.component.repo_needs_push())
        self.assertTrue(self.component.project.repo_needs_push())

        self.translation.do_push(self.get_request())

        self.assertFalse(self.translation.repo_needs_push())
        self.assertFalse(self.component.repo_needs_push())
        self.assertFalse(self.component.project.repo_needs_push())

    def test_edit_locked(self):
        self.component.locked = True
        self.component.save()
        response = self.edit_unit("Hello, world!\n", "Nazdar svete!\n", follow=True)
        # We should get to second message
        self.assertContains(
            response,
            "The translation is temporarily closed for contributions due "
            "to maintenance, please come back later.",
        )
        self.assert_backend(0)

    def test_edit_changed_source(self):
        # We use invalid contentsum here
        response = self.edit_unit(
            "Hello, world!\n", "Nazdar svete!\n", contentsum="aaa"
        )
        # We should get an error message
        self.assertContains(response, "The source string has changed meanwhile.")
        self.assert_backend(0)

    def test_edit_changed_translation(self):
        # We use invalid translationsum here
        response = self.edit_unit(
            "Hello, world!\n", "Nazdar svete!\n", translationsum="aaa"
        )
        # We should get an error message
        self.assertContains(
            response, "The translation of the string has changed meanwhile."
        )
        self.assert_backend(0)

    def test_edit_view(self):
        url = self.get_unit("Hello, world!\n").get_absolute_url()
        response = self.client.get(url)
        form = response.context["form"]
        params = {field: form[field].value() for field in form.fields}
        params["target_0"] = "Nazdar svete!\n"
        response = self.client.post(url, params)
        unit = self.get_unit()
        self.assertEqual(unit.target, "Nazdar svete!\n")
        self.assertEqual(unit.state, STATE_TRANSLATED)
        self.assert_backend(1)

    def test_remove_unit(self):
        self.component.manage_units = True
        self.component.save()
        self.user.is_superuser = True
        self.user.save()

        unit_count = Unit.objects.count()
        unit = self.get_unit()
        source_unit = unit.source_unit
        all_units = source_unit.unit_set.exclude(pk__in=[unit.pk, source_unit.pk])
        # Delete all other units
        for i, other in enumerate(all_units):
            response = self.client.post(
                reverse("delete-unit", kwargs={"unit_id": other.pk})
            )
            self.assertEqual(response.status_code, 302)
            self.assertEqual(unit_count - 1 - i, Unit.objects.count())
        # Deleting translation unit
        response = self.client.post(reverse("delete-unit", kwargs={"unit_id": unit.pk}))
        self.assertEqual(response.status_code, 302)

        # The source unit should be now removed as well
        self.assertFalse(Unit.objects.filter(pk=source_unit.pk).exists())
        self.assertEqual(unit_count - 4, Unit.objects.count())


class EditSourceTest(ViewTestCase):
    def create_component(self):
        return self.create_ts_mono()

    def test_edit_source_pending(self):
        old_revision = self.get_translation().revision

        # Edit source string
        self.edit_unit("Hello, world!\n", "Hello, beautiful world!\n", language="en")

        # Force commiting source string change
        self.component.commit_pending("test", None)

        # Translation revision should have been updated now
        self.assertNotEqual(old_revision, self.get_translation().revision)

        # Add translation
        self.edit_unit("Hello, beautiful world!\n", "Ahoj, světe!\n", language="cs")

        # Verify it has been stored in the database
        self.assertEqual(
            self.get_unit("Hello, beautiful world!\n", language="cs").target,
            "Ahoj, světe!\n",
        )

        # Check sync should be no-op now
        self.component.create_translations()

        # Check that translation was preserved
        self.assertEqual(
            self.get_unit("Hello, beautiful world!\n", language="cs").target,
            "Ahoj, světe!\n",
        )


class EditSourceAddonTest(EditSourceTest):
    def create_component(self):
        # This pulls in cleanup add-on
        return self.create_android()
