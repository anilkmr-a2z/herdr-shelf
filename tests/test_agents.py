import subprocess
import unittest

from shelf import agents

# Plain relaunch for session "S", as herdr's src/agent_resume.rs builds it.
EXPECTED = {
    "claude": ["claude", "--resume", "S"],
    "codex": ["codex", "resume", "S"],
    "copilot": ["copilot", "--resume=S"],
    "devin": ["devin", "--resume", "S"],
    "droid": ["droid", "--resume", "S"],
    "kimi": ["kimi", "--session", "S"],
    "mastracode": ["mastracode", "--thread", "S"],
    "pi": ["pi", "--session", "S"],
    "omp": ["omp", "--resume=S"],
    "hermes": ["hermes", "--resume", "S"],
    "opencode": ["opencode", "--session", "S"],
    "qodercli": ["qodercli", "--resume", "S"],
    "qwen": ["qwen", "--resume", "S"],
    "kilo": ["kilo", "--session", "S"],
    "cursor": ["cursor-agent", "--resume", "S"],
    "agy": ["agy", "--conversation", "S"],
    "grok": ["grok", "--resume", "S"],
    "letta": ["letta", "--conversation", "S"],
}


class TableTest(unittest.TestCase):
    def test_every_builtin_plain_relaunch_matches_herdr(self):
        t = agents.table()
        self.assertEqual(set(t), set(EXPECTED))
        for name, argv in EXPECTED.items():
            with self.subTest(agent=name):
                self.assertEqual(agents.relaunch_argv(name, t[name], "S", None), argv)

    def test_letta_default_agent_form(self):
        t = agents.table()
        self.assertEqual(agents.relaunch_argv("letta", t["letta"], "default:agent-7", None),
                         ["letta", "--conversation", "default", "--agent", "agent-7"])

    def test_override_merges_adds_and_drops_incomplete(self):
        t = agents.table({
            "claude": {"relaunch": "plain"},
            "myagent": {"program": "myagent", "resume": ["--load", "{id}"]},
            "broken": {"relaunch": "plain"},
        })
        self.assertEqual(t["claude"]["program"], "claude")
        self.assertEqual(t["claude"]["relaunch"], "plain")
        self.assertEqual(agents.relaunch_argv("myagent", t["myagent"], "S", ["myagent", "-v"]),
                         ["myagent", "-v", "--load", "S"])
        self.assertNotIn("broken", t)

    def test_builtin_table_not_mutated_by_override(self):
        agents.table({"claude": {"program": "other"}})
        self.assertEqual(agents.BUILTIN["claude"]["program"], "claude")


class RelaunchTest(unittest.TestCase):
    def setUp(self):
        self.t = agents.table()

    def test_keeps_flags_and_strips_old_resume(self):
        saved = ["/usr/local/bin/claude", "--agent", "reviewer", "--resume", "OLD", "--effort", "max"]
        self.assertEqual(agents.relaunch_argv("claude", self.t["claude"], "NEW", saved),
                         ["claude", "--agent", "reviewer", "--effort", "max", "--resume", "NEW"])

    def test_strips_equals_form_aliases_and_bare_flags(self):
        saved = ["claude", "--resume=OLD", "-r", "OLD2", "-c", "--continue", "--model", "opus"]
        self.assertEqual(agents.relaunch_argv("claude", self.t["claude"], "NEW", saved),
                         ["claude", "--model", "opus", "--resume", "NEW"])

    def test_codex_subcommand_removed(self):
        saved = ["codex", "--model", "o4", "resume", "OLD"]
        self.assertEqual(agents.relaunch_argv("codex", self.t["codex"], "NEW", saved),
                         ["codex", "--model", "o4", "resume", "NEW"])

    def test_equals_template_strips_both_forms(self):
        saved = ["omp", "--resume", "OLD", "-r", "OLD2", "--fast"]
        self.assertEqual(agents.relaunch_argv("omp", self.t["omp"], "NEW", saved),
                         ["omp", "--fast", "--resume=NEW"])

    def test_plain_ignores_saved_argv(self):
        entry = dict(self.t["claude"], relaunch="plain")
        self.assertEqual(agents.relaunch_argv("claude", entry, "NEW", ["claude", "fix the build"]),
                         ["claude", "--resume", "NEW"])

    def test_argv0_never_stripped(self):
        self.assertEqual(agents.strip_resume(["-c", "x"], self.t["claude"]), ["-c", "x"])

    def test_claude_strips_session_id_and_fork_session(self):
        saved = ["claude", "--session-id", "OLD-ID", "--fork-session", "--model", "opus"]
        self.assertEqual(agents.relaunch_argv("claude", self.t["claude"], "NEW", saved),
                         ["claude", "--model", "opus", "--resume", "NEW"])


class OptionalValueTest(unittest.TestCase):
    """A value-taking flag or subcommand only consumes a following token that
    does not itself look like a flag, so a following option is not swallowed."""

    def setUp(self):
        self.t = agents.table()

    def test_value_flag_without_a_following_value_is_not_swallowed(self):
        saved = ["claude", "-r", "--effort", "max"]
        self.assertEqual(agents.relaunch_argv("claude", self.t["claude"], "NEW", saved),
                         ["claude", "--effort", "max", "--resume", "NEW"])

    def test_subcommand_without_a_following_value_is_not_swallowed(self):
        saved = ["codex", "resume", "-m", "o3"]
        self.assertEqual(agents.relaunch_argv("codex", self.t["codex"], "NEW", saved),
                         ["codex", "-m", "o3", "resume", "NEW"])


class ArgvZeroTest(unittest.TestCase):
    def setUp(self):
        self.t = agents.table()

    def test_rewritten_to_bare_program_name_when_basename_matches(self):
        saved = ["/opt/versioned/claude", "--model", "opus"]
        self.assertEqual(agents.relaunch_argv("claude", self.t["claude"], "NEW", saved),
                         ["claude", "--model", "opus", "--resume", "NEW"])

    def test_left_alone_for_a_wrapper(self):
        saved = ["node", "/opt/claude-cli.js", "--model", "opus"]
        self.assertEqual(agents.relaunch_argv("claude", self.t["claude"], "NEW", saved),
                         ["node", "/opt/claude-cli.js", "--model", "opus", "--resume", "NEW"])


class PromptGuardTest(unittest.TestCase):
    """A saved argv that still looks like it carries a prompt after stripping
    falls back to a plain relaunch, so the prompt is never sent again."""

    def setUp(self):
        self.t = agents.table()

    def test_positional_prompt_with_whitespace_forces_plain(self):
        saved = ["claude", "--dangerously-skip-permissions", "fix the build"]
        self.assertEqual(agents.relaunch_argv("claude", self.t["claude"], "NEW", saved),
                         ["claude", "--resume", "NEW"])

    def test_codex_positional_prompt_forces_plain(self):
        saved = ["codex", "resume", "OLD", "keep going"]
        self.assertEqual(agents.relaunch_argv("codex", self.t["codex"], "NEW", saved),
                         ["codex", "resume", "NEW"])

    def test_double_dash_forces_plain(self):
        saved = ["claude", "--", "foo"]
        self.assertEqual(agents.relaunch_argv("claude", self.t["claude"], "NEW", saved),
                         ["claude", "--resume", "NEW"])

    def test_flags_without_whitespace_are_kept(self):
        saved = ["claude", "--effort", "max"]
        self.assertEqual(agents.relaunch_argv("claude", self.t["claude"], "NEW", saved),
                         ["claude", "--effort", "max", "--resume", "NEW"])

    def test_warns_when_falling_back(self):
        saved = ["claude", "fix the build"]
        with self.assertLogs("shelf", level="WARNING"):
            agents.relaunch_argv("claude", self.t["claude"], "NEW", saved)


class ValidSessionValueTest(unittest.TestCase):
    def test_empty_or_non_string_is_invalid(self):
        self.assertFalse(agents.valid_session_value("claude", ""))
        self.assertFalse(agents.valid_session_value("claude", None))
        self.assertFalse(agents.valid_session_value("claude", 123))

    def test_too_long_is_invalid(self):
        self.assertFalse(agents.valid_session_value("claude", "x" * 513))
        self.assertTrue(agents.valid_session_value("claude", "x" * 512))

    def test_control_characters_are_invalid(self):
        self.assertFalse(agents.valid_session_value("claude", "abc\ndef"))
        self.assertFalse(agents.valid_session_value("claude", "abc\x7fdef"))

    def test_leading_dash_is_invalid(self):
        self.assertFalse(agents.valid_session_value("claude", "-rf"))

    def test_ordinary_value_is_valid(self):
        self.assertTrue(agents.valid_session_value("claude", "abc-123"))

    def test_letta_default_form_needs_a_non_empty_remainder(self):
        self.assertTrue(agents.valid_session_value("letta", "default:agent-7"))
        self.assertFalse(agents.valid_session_value("letta", "default:"))

    def test_relaunch_argv_raises_for_invalid_value(self):
        t = agents.table()
        with self.assertRaises(ValueError):
            agents.relaunch_argv("claude", t["claude"], "-rf", None)
        with self.assertRaises(ValueError):
            agents.relaunch_argv("claude", t["claude"], "", None)


class ProgramMatchTest(unittest.TestCase):
    def test_basename(self):
        entry = agents.table()["cursor"]
        self.assertTrue(agents.matches_program(entry, "/opt/bin/cursor-agent"))
        self.assertFalse(agents.matches_program(entry, "/opt/bin/cursor"))


class ShellCommandTest(unittest.TestCase):
    def test_quoting_survives_the_shell(self):
        argv = ["printf", "%s|", "it's", "a b", '"q"', "$HOME"]
        cmd = agents.shell_command(argv)
        self.assertEqual(cmd[:2], ["sh", "-c"])
        self.assertTrue(cmd[2].startswith("trap : INT; "))
        self.assertTrue(cmd[2].endswith('; exec "${SHELL:-sh}"'))
        script = cmd[2][len("trap : INT; ") : -len('; exec "${SHELL:-sh}"')]
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, 'it\'s|a b|"q"|$HOME|')


if __name__ == "__main__":
    unittest.main()
