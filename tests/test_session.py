import unittest

from shelf import session


class HerdrSessionNameTest(unittest.TestCase):
    def test_default_socket_path_is_the_default_session(self):
        self.assertEqual(session.herdr_session_name("/home/user/.config/herdr/herdr.sock"), "default")

    def test_named_session_socket_path(self):
        self.assertEqual(
            session.herdr_session_name("/home/user/.config/herdr/sessions/cao/herdr.sock"), "cao")

    def test_missing_socket_path_is_the_default_session(self):
        self.assertEqual(session.herdr_session_name(None), "default")
        self.assertEqual(session.herdr_session_name(""), "default")

    def test_trailing_slash_on_a_named_session_socket_path(self):
        self.assertEqual(
            session.herdr_session_name("/home/user/.config/herdr/sessions/cao/herdr.sock/"), "cao")

    def test_trailing_slashes_on_a_named_session_socket_path(self):
        self.assertEqual(
            session.herdr_session_name("/home/user/.config/herdr/sessions/cao/herdr.sock//"), "cao")

    def test_session_name_with_dots_and_dashes(self):
        self.assertEqual(
            session.herdr_session_name("/x/sessions/my-session.1/herdr.sock"), "my-session.1")

    # -- Only the exact ".../sessions/<name>/herdr.sock" tail is special
    # ("sessions" as the third-from-last component). Anything else --
    # including a path that merely has a "sessions" component somewhere
    # else, or the tail shape but no room for a name -- resolves to
    # "default" rather than being disabled. --

    def test_sessions_directory_without_a_name_is_default(self):
        # No <name> segment between "sessions" and "herdr.sock": "sessions"
        # ends up second-from-last, not third-from-last, so the tail
        # pattern does not match at all.
        self.assertEqual(session.herdr_session_name("/home/user/.config/herdr/sessions/herdr.sock"), "default")

    def test_wrong_socket_filename_under_sessions_is_default(self):
        # The filename itself isn't "herdr.sock", so the tail pattern does
        # not match, regardless of "sessions" appearing in the path.
        self.assertEqual(
            session.herdr_session_name("/home/user/.config/herdr/sessions/cao/other.sock"), "default")

    def test_double_slash_collapsing_the_name_is_default(self):
        # os.path.normpath collapses the doubled slash, leaving "sessions"
        # immediately followed by "herdr.sock" with no name in between --
        # same as the no-name case above.
        self.assertEqual(session.herdr_session_name("/x/sessions//herdr.sock"), "default")

    def test_name_with_a_path_separator_is_default(self):
        # "sessions" is not the third-from-last component here ("b" is
        # second-from-last, "a" third-from-last), so the tail pattern does
        # not match; there is no "<name>" slot to even validate.
        self.assertEqual(session.herdr_session_name("/x/sessions/a/b/herdr.sock"), "default")

    def test_unrelated_ancestor_directory_named_sessions_is_default(self):
        # "sessions" here is nowhere near the end of the path -- an
        # ordinary ancestor directory that happens to share the name, not
        # herdr's own sessions/<name>/herdr.sock shape.
        self.assertEqual(session.herdr_session_name("/data/sessions/xdg/herdr/herdr.sock"), "default")

    def test_name_containing_dots_but_not_only_dots_is_accepted(self):
        # Only the exact names "." and ".." are reserved; a name that merely
        # contains dots is an ordinary, valid name.
        self.assertEqual(session.herdr_session_name("/x/sessions/..sneaky../herdr.sock"), "..sneaky..")

    def test_name_too_long_is_none(self):
        self.assertIsNone(session.herdr_session_name("/x/sessions/" + "a" * 65 + "/herdr.sock"))

    def test_name_exactly_max_length_is_accepted(self):
        name = "a" * 64
        self.assertEqual(session.herdr_session_name(f"/x/sessions/{name}/herdr.sock"), name)

    def test_name_with_invalid_characters_is_none(self):
        self.assertIsNone(session.herdr_session_name("/x/sessions/my session/herdr.sock"))
        self.assertIsNone(session.herdr_session_name("/x/sessions/my@session/herdr.sock"))

    # -- normpath is applied first, so ".." and doubled slashes in the
    # *surrounding* path (not the name itself) resolve the way a real
    # filesystem path would. --

    def test_dotdot_that_normalizes_sessions_away_falls_back_to_default(self):
        # "sessions/.." cancels out under normpath, so the resulting path no
        # longer names a session at all.
        self.assertEqual(session.herdr_session_name("/x/sessions/../herdr.sock"), "default")

    def test_dotdot_after_the_name_collapses_to_default(self):
        # "cao/.." cancels out, leaving "sessions" directly before
        # "herdr.sock" with no name -- same as the no-name case above, so
        # this resolves to "default" rather than being disabled.
        self.assertEqual(session.herdr_session_name("/x/sessions/cao/../herdr.sock"), "default")

    def test_doubled_slashes_elsewhere_are_tolerated(self):
        self.assertEqual(session.herdr_session_name("/x//sessions/cao/herdr.sock"), "cao")

    def test_relative_named_session_path(self):
        self.assertEqual(session.herdr_session_name("sessions/cao/herdr.sock"), "cao")

    def test_relative_path_with_leading_dotdot(self):
        self.assertEqual(session.herdr_session_name("../sessions/cao/herdr.sock"), "cao")

    def test_relative_default_path(self):
        self.assertEqual(session.herdr_session_name("herdr.sock"), "default")


class ValidSessionNameTest(unittest.TestCase):
    """os.path.normpath resolves away a literal "." or ".." *component* of
    the surrounding path before a name is ever extracted (see the dotdot
    tests above), so this rejection is only reachable by calling the
    validation helper directly -- tested here as a defensive check on that
    helper itself, matching herdr's own session name rule."""

    def test_dot_and_dotdot_are_rejected(self):
        self.assertFalse(session._valid_session_name("."))
        self.assertFalse(session._valid_session_name(".."))

    def test_ordinary_names_are_accepted(self):
        for name in ("default", "cao", "my-session.1", "a" * 64):
            self.assertTrue(session._valid_session_name(name))

    def test_empty_and_too_long_are_rejected(self):
        self.assertFalse(session._valid_session_name(""))
        self.assertFalse(session._valid_session_name("a" * 65))

    def test_non_string_is_rejected(self):
        self.assertFalse(session._valid_session_name(None))
        self.assertFalse(session._valid_session_name(123))


if __name__ == "__main__":
    unittest.main()
