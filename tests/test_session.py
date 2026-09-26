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

    def test_sessions_directory_without_a_name_is_the_default_session(self):
        # No <name> segment between "sessions" and "herdr.sock".
        self.assertEqual(session.herdr_session_name("/home/user/.config/herdr/sessions/herdr.sock"), "default")

    def test_wrong_socket_filename_is_the_default_session(self):
        self.assertEqual(
            session.herdr_session_name("/home/user/.config/herdr/sessions/cao/other.sock"), "default")

    def test_session_name_with_dots_and_dashes(self):
        self.assertEqual(
            session.herdr_session_name("/x/sessions/my-session.1/herdr.sock"), "my-session.1")


if __name__ == "__main__":
    unittest.main()
