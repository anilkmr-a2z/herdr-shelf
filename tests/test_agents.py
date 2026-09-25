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
                         ["/usr/local/bin/claude", "--agent", "reviewer", "--effort", "max", "--resume", "NEW"])

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
        self.assertTrue(cmd[2].endswith('; exec "${SHELL:-sh}"'))
        script = cmd[2][: -len('; exec "${SHELL:-sh}"')]
        out = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, 'it\'s|a b|"q"|$HOME|')


if __name__ == "__main__":
    unittest.main()
