import os
import signal
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shelf import archive, picker
from shelf.util import FileLock, LockBusy, iso

LOCAL = timezone(timedelta(hours=-7))  # the "local" zone for every test here
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=LOCAL)  # 17:00Z
TODAY_AT = "2026-09-29T16:14:00Z"  # 09:14 local
WEEK_AT = "2026-09-25T21:41:17Z"
MONTH_AT = "2026-09-10T12:00:00Z"
OLDER_AT = "2026-08-01T12:00:00Z"


def rec(archive_id, archived_at, label, workspace="backend", agent="claude", idle=20, cwd=None):
    last = iso(NOW - timedelta(days=idle, hours=1))
    return {"id": archive_id, "archived_at": archived_at, "tab": {"label": label},
            "workspace": {"label": workspace, "cwd": cwd},
            "panes": {"p": {"agent": agent, "last_activity": last}}}


HOME = "/home/user"  # RenderTest sets $HOME to this
HOME_SRC = HOME + "/src/backend"
ELEVEN = [
    rec("t1", TODAY_AT, "perf-probe", idle=21),
    rec("t2", TODAY_AT, "flaky-test", agent="codex", idle=15, cwd=HOME_SRC),
    rec("w1", WEEK_AT, "onboarding", workspace="docs", idle=28),
    rec("w2", WEEK_AT, "api-refactor", idle=17),
    rec("w3", WEEK_AT, "release-notes", workspace="docs", idle=18),
    rec("m1", MONTH_AT, "db-migration", idle=34),
    rec("m2", "2026-09-09T12:00:00Z", "style-guide", workspace="docs", idle=42),
    rec("o1", OLDER_AT, "lint-cleanup", idle=43),
    rec("o2", OLDER_AT, "api-sketch", idle=60),
    rec("o3", OLDER_AT, "old-spike", idle=32),
    rec("o4", "2026-07-01T12:00:00Z", "misc", idle=90),
]


def state_of(records=ELEVEN, height=16, width=80):
    state = picker.initial_state(records, NOW, LOCAL)
    state.height, state.width = height, width
    return state


def press(state, *events):
    action = None
    for event in events:
        action = picker.reduce(state, ("char", event) if isinstance(event, str) and len(event) == 1 else event)
    return action


def current(state):
    return picker.rows(state)[state.cursor]


def labels(state):
    return [r.group if r.kind == "header" else r.record["tab"]["label"] for r in picker.rows(state)]


class FakeArchive:
    def __init__(self, records, root):
        self.records = list(records)
        self.deleted = []
        self.root = root

    def list(self):
        return list(self.records)

    def delete(self, archive_id):
        self.deleted.append(archive_id)
        self.records = [r for r in self.records if r["id"] != archive_id]


class GroupTest(unittest.TestCase):
    def group(self, archived_at):
        return picker.group_of({"archived_at": archived_at}, NOW, LOCAL)

    def test_boundaries_in_local_calendar_days(self):
        cases = {
            "2026-09-29T07:00:00Z": picker.TODAY,  # 00:00 local today
            "2026-09-29T06:59:00Z": picker.WEEK,  # 23:59 local yesterday
            "2026-09-23T12:00:00Z": picker.WEEK,  # 6 days ago
            "2026-09-22T12:00:00Z": picker.MONTH,  # 7 days ago
            "2026-08-31T12:00:00Z": picker.MONTH,  # 29 days ago
            "2026-08-30T12:00:00Z": picker.OLDER,  # 30 days ago
            "2026-10-01T12:00:00Z": picker.TODAY,  # a clock that ran ahead
        }
        for archived_at, expected in cases.items():
            with self.subTest(archived_at=archived_at):
                self.assertEqual(self.group(archived_at), expected)

    def test_a_utc_timestamp_on_a_different_local_date(self):
        # 03:00Z on the 29th is 20:00 local on the 28th: yesterday, not today.
        self.assertEqual(self.group("2026-09-29T03:00:00Z"), picker.WEEK)

    def test_uses_the_time_zone_of_now_not_the_machine_s(self):
        # 18:20Z on the 28th is 00:05 on the 29th at +05:45: today there,
        # yesterday in the machine's own zone (unless that is +05:45 too).
        kathmandu = timezone(timedelta(hours=5, minutes=45))
        now = datetime(2026, 9, 29, 0, 30, tzinfo=kathmandu)
        self.assertEqual(picker.group_of({"archived_at": "2026-09-28T18:20:00Z"}, now, kathmandu), picker.TODAY)

    def test_missing_or_unparseable_archived_at_is_older(self):
        for value in (None, "", "not-a-date"):
            with self.subTest(value=value):
                self.assertEqual(self.group(value), picker.OLDER)


class InitialStateTest(unittest.TestCase):
    def test_older_starts_collapsed_and_the_rest_open(self):
        self.assertEqual(labels(state_of()), [
            picker.TODAY, "flaky-test", "perf-probe",
            picker.WEEK, "api-refactor", "release-notes", "onboarding",
            picker.MONTH, "db-migration", "style-guide",
            picker.OLDER])

    def test_sorted_newest_archive_first_then_least_idle_first(self):
        # Within Today both came from one sweep, so the less idle one leads;
        # within Last 30 days the more recently archived one leads.
        rs = picker.rows(state_of())
        self.assertEqual([r.record["id"] for r in rs if r.kind == "tab"][:2], ["t2", "t1"])
        self.assertEqual([r.record["id"] for r in rs if r.group == picker.MONTH and r.kind == "tab"],
                         ["m1", "m2"])

    def test_a_newer_archive_leads_even_when_it_is_more_idle(self):
        newer = rec("a", "2026-09-10T12:00:00Z", "newer", idle=40)
        older = rec("b", "2026-09-09T12:00:00Z", "older", idle=30)
        self.assertEqual(labels(state_of([older, newer])), [picker.MONTH, "newer", "older"])

    def test_an_entry_with_no_activity_sorts_after_its_sweep_mates(self):
        blank = dict(rec("x", TODAY_AT, "blank"), panes={})
        self.assertEqual(labels(state_of([blank, rec("y", TODAY_AT, "busy")])),
                         [picker.TODAY, "busy", "blank"])

    def test_older_opens_when_it_is_the_only_group(self):
        state = state_of([r for r in ELEVEN if r["id"].startswith("o")])
        self.assertEqual(labels(state), [picker.OLDER, "old-spike", "lint-cleanup", "api-sketch", "misc"])

    def test_empty_groups_are_not_shown(self):
        state = state_of([r for r in ELEVEN if r["id"] in ("t1", "o1")])
        self.assertEqual(labels(state), [picker.TODAY, "perf-probe", picker.OLDER])

    def test_cursor_starts_on_the_first_tab_row(self):
        self.assertEqual(current(state_of()).record["id"], "t2")

    def test_no_activity_timestamp_still_lists_the_entry(self):
        entry = dict(rec("x", TODAY_AT, "blank"), panes={})
        state = state_of([entry])
        self.assertEqual(labels(state), [picker.TODAY, "blank"])


class MoveTest(unittest.TestCase):
    def test_up_and_down_stop_at_the_ends(self):
        state = state_of()
        press(state, "up", "up", "up")
        self.assertEqual(state.cursor, 0)
        press(state, *["down"] * 20)
        self.assertEqual(current(state).group, picker.OLDER)

    def test_vim_keys_move_like_the_arrows(self):
        state = state_of()
        press(state, "j")
        self.assertEqual(current(state).record["id"], "t1")
        press(state, "k")
        self.assertEqual(current(state).record["id"], "t2")

    def test_page_home_and_end(self):
        state = state_of()
        press(state, "end", "right", "home", "pgdn")  # all 15 rows showing
        self.assertEqual(state.cursor, 10)
        press(state, "pgdn", "pgup")  # 10 -> 14 (the end) -> 4
        self.assertEqual(state.cursor, 4)
        press(state, "end")
        self.assertEqual(state.cursor, len(picker.rows(state)) - 1)
        press(state, "home")
        self.assertEqual(state.cursor, 0)

    def test_enter_on_a_tab_restores_it(self):
        self.assertEqual(press(state_of(), "enter"), ("restore", "t2"))

    def test_enter_on_a_header_toggles_the_group(self):
        state = state_of()
        press(state, "end", "enter")
        self.assertIn("old-spike", labels(state))
        press(state, "enter")
        self.assertNotIn("old-spike", labels(state))

    def test_right_opens_and_left_closes(self):
        state = state_of()
        press(state, "end", "l")
        self.assertIn("old-spike", labels(state))
        press(state, "right")  # already open: stays open
        self.assertIn("old-spike", labels(state))
        press(state, "left")
        self.assertNotIn("old-spike", labels(state))

    def test_left_on_a_tab_jumps_to_its_header_and_closes_it(self):
        state = state_of()
        press(state, "down", "down", "down")  # api-refactor, under Last 7 days
        press(state, "h")
        self.assertEqual(current(state), picker.Row("header", picker.WEEK, None, 3, False))
        self.assertNotIn("api-refactor", labels(state))

    def test_q_and_esc_quit(self):
        self.assertEqual(press(state_of(), "q"), ("quit",))
        self.assertEqual(press(state_of(), "esc"), ("quit",))

    def test_moving_past_the_window_scrolls_one_row(self):
        state = state_of()
        press(state, "end", "right", "home", *["down"] * 10)
        self.assertEqual((state.cursor, state.offset), (10, 1))

    def test_closing_a_group_while_scrolled_pulls_the_window_back(self):
        state = state_of()
        press(state, "end", "right", "end")  # (14, 5)
        press(state, "left")
        self.assertEqual((state.cursor, state.offset), (10, 1))

    def test_resize_to_a_shorter_popup_keeps_the_cursor_in_view(self):
        state = state_of()
        press(state, *["down"] * 8)  # row 9
        picker.reduce(state, ("resize", 8, 80))  # 6 list rows
        self.assertEqual((state.cursor, state.offset), (9, 4))

    def test_scrolling_keeps_the_cursor_in_the_ten_row_window(self):
        state = state_of()
        press(state, "end", "right")  # all 15 rows showing
        press(state, "end")
        self.assertEqual((state.cursor, state.offset), (14, 5))
        press(state, *["up"] * 9)
        self.assertEqual((state.cursor, state.offset), (5, 5))
        press(state, "up")
        self.assertEqual((state.cursor, state.offset), (4, 4))

    def test_wheel_moves_three_rows(self):
        state = state_of()
        press(state, "wheeldown")
        self.assertEqual(state.cursor, 4)
        press(state, "wheelup")
        self.assertEqual(state.cursor, 1)

    def test_click_highlights_and_double_click_restores(self):
        state = state_of()
        self.assertIsNone(press(state, ("click", 5)))
        self.assertEqual(current(state).record["id"], "w3")
        self.assertEqual(press(state, ("dclick", 5)), ("restore", "w3"))

    def test_click_on_a_header_toggles_it(self):
        state = state_of()
        press(state, ("click", 0))
        self.assertEqual(current(state).group, picker.TODAY)
        self.assertEqual(current(state).kind, "header")
        self.assertNotIn("flaky-test", labels(state))

    def test_double_click_on_a_header_toggles_it_without_restoring(self):
        state = state_of()
        self.assertIsNone(press(state, ("dclick", 0)))
        self.assertNotIn("flaky-test", labels(state))

    def test_click_accounts_for_the_scroll_offset_and_ignores_empty_rows(self):
        state = state_of()
        press(state, "end", "right", "end")  # offset 5
        press(state, ("click", 0))
        self.assertEqual(current(state).record["id"], "w3")
        state = state_of()
        self.assertIsNone(press(state, ("click", 9)))  # rows 0-10 exist; row 9 is style-guide
        self.assertEqual(current(state).record["id"], "m2")
        self.assertIsNone(press(state, ("click", 11)))
        self.assertEqual(current(state).record["id"], "m2")


class WindowRowTest(unittest.TestCase):
    def test_maps_screen_lines_to_list_rows(self):
        state = state_of(height=16)
        self.assertEqual([picker.window_row(state, y) for y in (1, 2, 11, 12)], [None, 0, 9, None])
        state = state_of(height=13)  # no markers: the list starts right under the title
        self.assertEqual(picker.window_row(state, 1), 0)

    def test_no_list_to_click_on_a_popup_that_is_too_small(self):
        self.assertIsNone(picker.window_row(state_of(height=4), 2))
        self.assertIsNone(picker.window_row(state_of(width=29), 3))


class FilterTest(unittest.TestCase):
    def test_typing_narrows_and_opens_groups_with_a_match(self):
        state = state_of()
        press(state, "/", "a", "p", "i")
        self.assertEqual(state.mode, "filter")
        self.assertEqual(labels(state), [picker.WEEK, "api-refactor", picker.OLDER, "api-sketch"])
        self.assertEqual(current(state).record["id"], "w2")

    def test_matches_workspace_and_agent_names_case_insensitively(self):
        state = state_of()
        press(state, "/", "C", "O", "D", "E", "X")
        self.assertEqual(labels(state), [picker.TODAY, "flaky-test"])
        press(state, "esc", "/", "d", "o", "c", "s")
        self.assertEqual(set(labels(state)) - set(picker.GROUPS),
                         {"onboarding", "release-notes", "style-guide"})

    def test_filter_keys_are_typed_as_text(self):
        state = state_of()
        self.assertIsNone(press(state, "/", "y", "q", "d", "j", "k", "h", "l", "/"))
        self.assertEqual((state.mode, state.filter_text), ("filter", "yqdjkhl/"))

    def test_no_match(self):
        state = state_of()
        press(state, "/", "z", "z", "z")
        self.assertEqual(picker.rows(state), [])

    def test_enter_keeps_the_filter_then_esc_clears_it_then_esc_quits(self):
        state = state_of()
        press(state, "/", "a", "p", "i", "enter")
        self.assertEqual((state.mode, state.filter_text), ("move", "api"))
        self.assertEqual(press(state, "down"), None)
        self.assertEqual(current(state).group, picker.OLDER)
        self.assertIsNone(press(state, "esc"))
        self.assertEqual(state.filter_text, "")
        self.assertEqual(press(state, "esc"), ("quit",))

    def test_backspace_deletes_then_leaves_filter_mode(self):
        state = state_of()
        press(state, "/", "a", "backspace")
        self.assertEqual((state.mode, state.filter_text), ("filter", ""))
        press(state, "backspace")
        self.assertEqual(state.mode, "move")

    def test_clearing_restores_the_open_groups_from_before_the_filter(self):
        for exit_keys in (("esc",), ("enter", "esc"), ("backspace",) * 3):
            with self.subTest(exit_keys=exit_keys):
                state = state_of()
                press(state, "home", "left", "end", "right")  # Today closed, Older opened, by hand
                before = labels(state)
                press(state, "/", "a", "p", "i", *exit_keys)
                self.assertEqual((state.filter_text, labels(state)), ("", before))

    def test_each_keystroke_scrolls_back_to_the_first_match(self):
        state = state_of()
        press(state, "end", "right", "end")  # (14, 5): scrolled to the bottom
        press(state, "/", "a")  # every entry matches "a", so all 15 rows show
        self.assertEqual((state.cursor, state.offset), (1, 0))

    def test_slash_edits_a_filter_that_matched_nothing(self):
        state = state_of()
        press(state, "/", "z", "enter", "/", "backspace")
        self.assertEqual((state.mode, state.filter_text), ("filter", ""))

    def test_esc_on_an_empty_filter_leaves_the_cursor_where_it_was(self):
        state = state_of()
        press(state, "end", "right", "end")  # misc, row 14
        press(state, "/", "esc")
        self.assertEqual((state.mode, state.cursor, state.offset), ("move", 14, 5))

    def test_a_click_while_typing_keeps_the_filter_and_acts(self):
        state = state_of()
        press(state, "/", "a", "p", "i")
        self.assertEqual(press(state, ("dclick", 1)), ("restore", "w2"))
        self.assertEqual((state.mode, state.filter_text), ("move", "api"))

    def test_clearing_the_filter_restores_the_open_groups(self):
        state = state_of()
        before = labels(state)
        press(state, "/", "a", "p", "i")  # opens Older
        press(state, "esc")
        self.assertEqual((state.mode, labels(state)), ("move", before))


class ConfirmTest(unittest.TestCase):
    def test_d_then_y_deletes(self):
        state = state_of()
        self.assertIsNone(press(state, "d"))
        self.assertEqual(state.mode, "confirm")
        self.assertEqual(press(state, "y"), ("delete", "t2"))
        self.assertEqual(state.mode, "move")

    def test_anything_else_cancels(self):
        for key in ("n", "N", "esc", "enter", "q", "down", "d", "/", "backspace", "other"):
            with self.subTest(key=key):
                state = state_of()
                press(state, "d")
                self.assertIsNone(press(state, key))
                self.assertEqual((state.mode, current(state).record["id"]), ("move", "t2"))

    def test_d_targets_the_highlighted_tab(self):
        self.assertEqual(press(state_of(), "down", "d", "y"), ("delete", "t1"))
        state = state_of()
        press(state, "/", "a", "p", "i", "enter", "down", "down")  # api-sketch, under Older
        self.assertEqual(press(state, "d", "y"), ("delete", "o2"))

    def test_only_closing_works_on_a_popup_that_is_too_small(self):
        state = state_of(height=4)  # the screen shows only "Popup too small"
        for key in ("enter", "d", "y", "down", "/"):
            with self.subTest(key=key):
                self.assertIsNone(press(state, key))
        self.assertEqual(state.mode, "move")
        self.assertEqual(press(state, "q"), ("quit",))
        self.assertEqual(press(state_of(width=29), "esc"), ("quit",))

    def test_d_on_a_header_does_nothing(self):
        state = state_of()
        press(state, "home", "d")
        self.assertEqual(state.mode, "move")


class RefreshTest(unittest.TestCase):
    def test_keeps_the_cursor_on_the_same_entry(self):
        state = state_of()
        press(state, "down", "down", "down")  # api-refactor
        picker.refresh(state, [r for r in ELEVEN if r["id"] not in ("t1", "t2")])
        self.assertEqual(current(state).record["id"], "w2")

    def test_a_cursor_on_a_header_stays_on_that_header(self):
        state = state_of()
        press(state, "end")  # the Older header
        picker.refresh(state, [r for r in ELEVEN if r["id"] != "t1"])
        self.assertEqual((current(state).kind, current(state).group), ("header", picker.OLDER))

    def test_a_group_that_appears_opens_like_at_start_up(self):
        state = state_of([r for r in ELEVEN if r["id"].startswith("o")])
        picker.refresh(state, ELEVEN)
        self.assertEqual(state.open_groups, set(picker.GROUPS))  # Older was already open

    def test_older_opens_when_it_becomes_the_only_group(self):
        state = state_of()
        picker.refresh(state, [r for r in ELEVEN if r["id"].startswith("o")])
        self.assertEqual(labels(state), [picker.OLDER, "old-spike", "lint-cleanup", "api-sketch", "misc"])

    def test_a_new_match_under_a_filter_is_shown_open(self):
        state = state_of([r for r in ELEVEN if r["id"] != "t1"])
        press(state, "/", "p", "r", "o", "b", "e", "enter")  # nothing matches yet
        picker.refresh(state, ELEVEN)  # a sweep brought perf-probe back
        self.assertEqual(labels(state), [picker.TODAY, "perf-probe"])

    def test_falls_back_to_the_same_position_when_the_entry_is_gone(self):
        state = state_of()
        press(state, "down")  # perf-probe, row 2
        picker.refresh(state, [r for r in ELEVEN if r["id"] != "t1"])
        self.assertEqual(state.cursor, 2)
        self.assertEqual(current(state).kind, "header")


class RenderTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"HOME": HOME})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_popup_as_it_opens(self):
        lines, highlight = picker.render(state_of())
        self.assertEqual(lines, [
            " Shelf - 11 archived",
            "",
            " - Archived today (2)",
            " > flaky-test                         backend                 codex        15d",
            "   perf-probe                         backend                 claude       21d",
            " - Last 7 days (3)",
            "   api-refactor                       backend                 claude       17d",
            "   release-notes                      docs                    claude       18d",
            "   onboarding                         docs                    claude       28d",
            " - Last 30 days (2)",
            "   db-migration                       backend                 claude       34d",
            "   style-guide                        docs                    claude       42d",
            " v 1 more",
            " ~/src/backend  shelved Sep 29 09:14  1 pane: codex",
            picker.KEYS_HINT[:79],
        ])
        self.assertEqual(highlight, 3)

    def test_scroll_markers(self):
        state = state_of()
        press(state, "end", "right", "end")
        lines, highlight = picker.render(state)
        self.assertEqual((lines[1], lines[12]), (" ^ 5 more", ""))
        self.assertEqual(highlight, 11)
        press(state, "home")
        lines, _ = picker.render(state)
        self.assertEqual((lines[1], lines[12]), ("", " v 5 more"))

    def test_details_line_counts_panes(self):
        two = dict(rec("x", TODAY_AT, "pair"), panes={"a": {"agent": "claude", "last_activity": iso(NOW)},
                                                      "b": {"agent": "codex", "last_activity": iso(NOW)}})
        lines, _ = picker.render(state_of([two]))
        self.assertTrue(lines[13].endswith("2 panes: claude,codex"), lines[13])

    def test_details_line_is_blank_on_a_header(self):
        state = state_of()
        press(state, "home")
        self.assertEqual(picker.render(state)[0][13], "")

    def test_narrow_popup_truncates_every_line(self):
        lines, _ = picker.render(state_of(width=40))
        self.assertTrue(all(len(line) <= 39 for line in lines))
        self.assertTrue(lines[3].endswith("15d"), lines[3])  # every column kept, just narrower

    def test_short_popups_drop_details_then_markers_then_list_rows(self):
        self.assertEqual(len(picker.render(state_of(height=15))[0]), 15)
        self.assertEqual(len(picker.render(state_of(height=14))[0]), 14)  # no details
        lines, _ = picker.render(state_of(height=12))  # no markers
        self.assertEqual((len(lines), lines[1]), (12, " - Archived today (2)"))
        self.assertEqual(len(picker.render(state_of(height=6))[0]), 6)

    def test_too_small(self):
        self.assertEqual(picker.render(state_of(height=4))[0], [picker.TOO_SMALL])
        self.assertEqual(picker.render(state_of(width=29))[0], [picker.TOO_SMALL])

    def test_no_match(self):
        state = state_of()
        press(state, "/", "z", "z", "z")
        lines, highlight = picker.render(state)
        self.assertEqual((lines[2], highlight), (" No match", None))

    def test_empty_archive(self):
        state = state_of([])
        self.assertEqual(picker.render(state)[0], ["No archived tabs.", "Press any key to close."])
        self.assertEqual(press(state, "x"), ("quit",))

    def test_status_line_for_confirm_filter_and_message(self):
        state = state_of()
        press(state, "d")
        self.assertEqual(picker.render(state)[0][-1],
                         ' Delete "flaky-test"? This cannot be undone. [y/N]')
        press(state, "n", "/", "a", "p")
        self.assertEqual(picker.render(state)[0][-1], " /ap")
        press(state, "enter")
        self.assertEqual(picker.render(state)[0][-1], " /ap  (Esc clears the filter)")
        state.message = "Restore failed: boom"
        self.assertEqual(picker.render(state)[0][-1], " Restore failed: boom")
        press(state, "down")  # a message lasts until the next key press
        self.assertEqual(picker.render(state)[0][-1], " /ap  (Esc clears the filter)")


class ApplyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shelf-picker-test-")
        self.addCleanup(tmp.cleanup)
        self.arch = FakeArchive(ELEVEN, Path(tmp.name) / "archive")
        self.state = state_of(self.arch.list())
        self.restored, self.notifications = [], []

    def apply(self, action, restore_error=None, warnings=()):
        def do_restore(archive_id):
            self.restored.append(archive_id)
            if restore_error:
                raise restore_error
            return {"tab_id": "t", "warnings": list(warnings)}

        return picker.apply(self.state, action, self.arch, do_restore,
                            lambda title, body: self.notifications.append((title, body)))

    def test_quit_and_no_action(self):
        self.assertTrue(self.apply(("quit",)))
        self.assertFalse(self.apply(None))

    def test_restore_closes_the_popup(self):
        self.assertTrue(self.apply(("restore", "t2")))
        self.assertEqual((self.restored, self.notifications), (["t2"], []))

    def test_restore_warnings_are_notified(self):
        self.assertTrue(self.apply(("restore", "t2"), warnings=["conversation missing"]))
        self.assertEqual(self.notifications, [("shelf", "conversation missing")])

    def test_restore_shows_its_progress_before_it_runs(self):
        seen = []

        def do_restore(archive_id):
            seen.append("restore")
            return {"warnings": []}

        picker.apply(self.state, ("restore", "t2"), self.arch, do_restore,
                     lambda *a: None, draw=lambda: seen.append(self.state.message))
        self.assertEqual(seen, ["Restoring flaky-test...", "restore"])

    def test_ctrl_c_is_ignored_during_a_restore_and_put_back_after(self):
        # Start from Python's default handler: a test process launched with
        # SIGINT already ignored would otherwise pass this without checking.
        self.addCleanup(signal.signal, signal.SIGINT, signal.signal(signal.SIGINT, signal.default_int_handler))
        before = signal.default_int_handler
        for error in (None, RuntimeError("boom"), LockBusy("x"), KeyboardInterrupt()):
            with self.subTest(error=error):
                during = []

                def do_restore(archive_id):
                    during.append(signal.getsignal(signal.SIGINT))
                    if error:
                        raise error
                    return {"warnings": []}

                try:
                    picker.apply(self.state, ("restore", "t2"), self.arch, do_restore, lambda *a: None)
                except KeyboardInterrupt:
                    pass
                self.assertEqual(during, [signal.SIG_IGN])
                self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_restore_does_not_hold_the_sweep_lock(self):
        # restore.restore takes sweep.lock itself; holding it here would make every restore time out.
        def do_restore(archive_id):
            with FileLock(self.arch.root.parent / "sweep.lock"):  # try once: LockBusy if the picker holds it
                return {"warnings": []}

        self.assertTrue(picker.apply(self.state, ("restore", "t2"), self.arch, do_restore, lambda *a: None))

    def test_a_failed_restore_re_reads_the_archive(self):
        def do_restore(archive_id):  # e.g. another popup restored it first
            self.arch.records = [r for r in self.arch.records if r["id"] != archive_id]
            raise KeyError(archive_id)

        self.assertFalse(picker.apply(self.state, ("restore", "t2"), self.arch, do_restore, lambda *a: None))
        self.assertNotIn("t2", [r.record["id"] for r in picker.rows(self.state) if r.kind == "tab"])

    def test_failed_restore_keeps_the_popup_and_the_entry(self):
        self.assertFalse(self.apply(("restore", "t2"), restore_error=RuntimeError("boom")))
        self.assertEqual(self.state.message, "Restore failed: boom")
        self.assertEqual(current(self.state).record["id"], "t2")

    def test_duplicate_conversation_skip_is_shown(self):
        skip = archive.Skip("conversation ab12cd34 is already open in another tab; close it first")
        self.assertFalse(self.apply(("restore", "t2"), restore_error=skip))
        self.assertIn("Restore failed: conversation ab12cd34 is already open", self.state.message)
        self.assertEqual(self.arch.deleted, [])

    def test_restore_lock_busy_is_friendly(self):
        lock_path = self.arch.root.parent / "sweep.lock"
        self.assertFalse(self.apply(("restore", "t2"), restore_error=LockBusy(str(lock_path))))
        self.assertEqual(self.state.message, picker.SWEEP_BUSY)

    def test_delete_removes_the_entry(self):
        self.assertFalse(self.apply(("delete", "t2")))
        self.assertEqual(self.arch.deleted, ["t2"])
        self.assertEqual(current(self.state).record["id"], "t1")

    def test_delete_shows_the_wait_then_clears_it(self):
        seen = []
        real_delete = self.arch.delete
        self.arch.delete = lambda archive_id: (seen.append("delete"), real_delete(archive_id))
        self.assertFalse(picker.apply(self.state, ("delete", "t2"), self.arch, None, None,
                                      draw=lambda: seen.append(self.state.message)))
        self.assertEqual(seen, ["Waiting for a sweep to finish...", "delete"])
        self.assertEqual(self.state.message, "")

    def test_delete_waits_for_a_sweep_to_release_the_lock(self):
        lock = FileLock(self.arch.root.parent / "sweep.lock").__enter__()
        timer = threading.Timer(0.2, lock.__exit__)
        timer.start()
        try:
            with mock.patch("shelf.picker.DELETE_LOCK_WAIT_SECONDS", 5):
                self.assertFalse(picker.apply(self.state, ("delete", "t2"), self.arch, None, None))
        finally:
            timer.join()
        self.assertEqual(self.arch.deleted, ["t2"])
        self.assertEqual(self.state.message, "")

    def test_delete_runs_under_the_sweep_lock(self):
        with FileLock(self.arch.root.parent / "sweep.lock"), \
                mock.patch("shelf.picker.DELETE_LOCK_WAIT_SECONDS", 0.1):
            self.assertFalse(self.apply(("delete", "t2")))
        self.assertEqual(self.arch.deleted, [])
        self.assertEqual(self.state.message, picker.SWEEP_BUSY)


class FakeCursesError(Exception):
    pass


class FakeScreen:
    def __init__(self, *keys):
        self.keys, self.timeouts = list(keys), []

    def get_wch(self):
        if not self.keys:
            raise FakeCursesError("no input")
        return self.keys.pop(0)

    def timeout(self, ms):
        self.timeouts.append(ms)

    def getmaxyx(self):
        return (16, 80)


def fake_curses(mouse=None, button4=0x10000):
    # ncurses 6 values (mouse version 2); the tests only need them to be distinct.
    return SimpleNamespace(
        error=FakeCursesError, KEY_RESIZE=410, KEY_MOUSE=409, KEY_UP=259, KEY_DOWN=258, KEY_LEFT=260,
        KEY_RIGHT=261, KEY_PPAGE=339, KEY_NPAGE=338, KEY_HOME=262, KEY_END=360, KEY_ENTER=343,
        KEY_BACKSPACE=263, BUTTON1_RELEASED=0x1, BUTTON1_CLICKED=0x4, BUTTON1_DOUBLE_CLICKED=0x8,
        BUTTON4_PRESSED=button4, BUTTON5_PRESSED=0x200000, getmouse=lambda: mouse)


class ReadEventTest(unittest.TestCase):
    """_read_event and _mouse_event take the curses module as an argument, so a stand-in drives them."""

    def read(self, *keys, mouse=None):
        return picker._read_event(fake_curses(mouse), FakeScreen(*keys), state_of())

    def test_keys(self):
        cases = {("\n",): "enter", ("\r",): "enter", ("\x7f",): "backspace", ("\b",): "backspace",
                 ("x",): ("char", "x"), ("\t",): "other", (258,): "down", (338,): "pgdn", (343,): "enter",
                 (263,): "backspace", (999,): "other", (410,): ("resize", 16, 80),
                 ("\x1b",): "esc", ("\x1b", "[", "B"): "down", ("\x1b", "O", "H"): "home",
                 ("\x1b", "[", "5"): "other", ("\x1b", "j"): "other", ("\x1b", "[", "1", ";", "5", "A"): "up"}
        for keys, expected in cases.items():
            with self.subTest(keys=keys):
                self.assertEqual(self.read(*keys), expected)

    def test_after_an_esc_the_read_blocks_again(self):
        for keys in (("\x1b",), ("\x1b", "[", "B")):
            with self.subTest(keys=keys):
                screen = FakeScreen(*keys)
                picker._read_event(fake_curses(), screen, state_of())
                self.assertEqual(screen.timeouts[-1], -1)

    def test_mouse(self):
        # Screen line 5 of a 16-row popup is list row 3; line 1 is the up marker.
        cases = {(5, 0x8): ("dclick", 3), (5, 0x4): ("click", 3), (5, 0x1): ("click", 3),
                 (5, 0x10000): "wheelup", (5, 0x200000): "wheeldown", (5, 0x2): None, (1, 0x4): None}
        for (y, buttons), expected in cases.items():
            with self.subTest(y=y, buttons=hex(buttons)):
                self.assertEqual(self.read(409, mouse=(0, 10, y, 0, buttons)), expected)

    def test_wheel_down_without_button5_pressed(self):
        # Python 3.9 does not export BUTTON5_PRESSED: use ncurses' mouse-version-2 value,
        # and give up on version 1 (BUTTON4_PRESSED == 0x80000), which has no wheel-down.
        for button4, expected in ((0x10000, "wheeldown"), (0x80000, None)):
            with self.subTest(button4=hex(button4)):
                curses = fake_curses((0, 10, 5, 0, 0x200000), button4=button4)
                del curses.BUTTON5_PRESSED
                self.assertEqual(picker._mouse_event(curses, state_of()), expected)


@unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
class MachineTimeZoneTest(unittest.TestCase):
    """With no tz, each archived_at is read with the DST offset of its own date."""

    def setUp(self):
        old = os.environ.get("TZ")

        def put_back():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

        self.addCleanup(put_back)
        os.environ["TZ"] = "America/Los_Angeles"  # DST ends on 2026-11-01
        time.tzset()

    def test_the_shelved_time_uses_the_offset_of_its_own_date(self):
        record = rec("x", "2026-10-30T16:14:00Z", "dst", cwd="/srv")  # 09:14 PDT
        now = datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc).astimezone()  # PST, a fixed -08:00
        state = picker.initial_state([record], now)
        state.height, state.width = 16, 80
        self.assertIn("shelved Oct 30 09:14", picker.render(state)[0][13])

    def test_the_group_uses_the_offset_of_its_own_date(self):
        # 07:30Z on Nov 1 is 00:30 PDT, before the switch: today, not yesterday, at 22:59 PST.
        now = datetime(2026, 11, 2, 6, 59, tzinfo=timezone.utc).astimezone()
        self.assertEqual(picker.group_of({"archived_at": "2026-11-01T07:30:00Z"}, now), picker.TODAY)



if __name__ == "__main__":
    unittest.main()
