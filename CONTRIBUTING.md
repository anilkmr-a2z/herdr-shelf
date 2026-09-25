# Contributing

Issues and pull requests are welcome.

- Run the tests before sending a change:
  `python3 -m unittest discover -s tests -t . -v`
- Keep the plugin dependency-free: Python 3.9 standard library only.
- To support a new agent, add a row to `BUILTIN` in `shelf/agents.py` and a case
  to `EXPECTED` in `tests/test_agents.py`. The row should match herdr's
  `src/agent_resume.rs` for that agent. A history reader in `shelf/history.py`
  is optional.
- Test against a real herdr server before a release: a dry-run sweep, then one
  archive and restore in live mode.
