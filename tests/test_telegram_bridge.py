"""The Telegram bridge as a file-protocol adapter. No network, ever.

The real ``TelegramClient`` is replaced wholesale by a fake with the same method
surface, so every test here is about the *file* side of the adapter: what moves
to ``outbox/archive/``, what is quarantined in ``outbox/failed/``, and what an
untrusted ``/c <project>`` message is allowed to create.

``tools/telegram-bridge.py`` is not an importable module name, so it is loaded
by path through ``importlib``.
"""

from __future__ import annotations

import json

from collabkit import frontmatter, slug
from collabkit.paths import CollabPaths
from collabkit.registry import Registry

from tests.support import (
    FakeTelegramClient,
    IsolatedHomeTestCase,
    load_bridge_module,
    telegram_message,
)

bridge_module = load_bridge_module()
TelegramError = bridge_module.TelegramError
CHAT_ID = 555


class SplitMessageTests(IsolatedHomeTestCase):
    """Telegram rejects anything over 4096 characters outright."""

    LIMIT = bridge_module.TELEGRAM_MAX_CHARS

    def test_a_short_message_is_a_single_chunk(self):
        self.assertEqual(list(bridge_module._split_message("hello")), ["hello"])

    def test_no_chunk_ever_exceeds_the_limit(self):
        cases = {
            "one enormous line": "x" * (self.LIMIT * 3 + 17),
            "many short lines": "\n".join(f"line {i}" for i in range(5000)),
            "mixed": ("y" * (self.LIMIT + 5) + "\n") + "\n".join("z" * 80 for _ in range(300)),
            "exactly the limit": "q" * self.LIMIT,
            "one over the limit": "q" * (self.LIMIT + 1),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                chunks = list(bridge_module._split_message(text))
                self.assertTrue(chunks)
                for chunk in chunks:
                    self.assertLessEqual(len(chunk), self.LIMIT)

    def test_line_boundaries_are_preserved_when_they_can_be(self):
        lines = [f"line {index} " + "y" * 60 for index in range(400)]
        text = "\n".join(lines)

        chunks = list(bridge_module._split_message(text))

        self.assertGreater(len(chunks), 1, "the fixture must actually need splitting")
        rejoined = []
        for chunk in chunks:
            rejoined.extend(chunk.split("\n"))
        self.assertEqual(rejoined, lines, "no line may be cut in half")

    def test_a_single_over_long_line_is_hard_split(self):
        text = "x" * (self.LIMIT * 2 + 100)
        chunks = list(bridge_module._split_message(text))
        self.assertEqual([len(chunk) for chunk in chunks], [self.LIMIT, self.LIMIT, 100])
        self.assertEqual("".join(chunks), text)

    def test_nothing_is_dropped_when_splitting(self):
        text = "\n".join(f"row {index}" for index in range(4000))
        chunks = list(bridge_module._split_message(text))
        self.assertEqual("\n".join(chunks), text.rstrip())


class BridgeTestCase(IsolatedHomeTestCase):
    def setUp(self):
        super().setUp()
        self.layout = self.home_paths()
        self.client = FakeTelegramClient()

    def make_bridge(self, *, client=None, pinned=CHAT_ID, **kwargs):
        return bridge_module.Bridge(
            client or self.client, self.layout, pinned_chat_id=pinned, **kwargs
        )

    def queue(self, name: str, body: str, **meta) -> "object":
        path = self.layout.outbox / name
        path.write_text(frontmatter.dumps(meta, body), encoding="utf-8")
        return path


class OutboxDeliveryTests(BridgeTestCase):
    """A file leaves the queue only after Telegram says ok."""

    def test_a_delivered_file_moves_to_the_archive(self):
        path = self.queue("m1.md", "hello from the agent", project="demo")
        bridge = self.make_bridge()

        delivered = bridge.pump_outbox()

        self.assertEqual(delivered, 1)
        self.assertEqual(self.client.texts, ["[demo] hello from the agent"])
        self.assertFalse(path.exists())
        self.assertTrue((self.layout.outbox_archive / "m1.md").is_file())

    def test_files_are_sent_oldest_first(self):
        for name in ("c.md", "a.md", "b.md"):
            self.queue(name, f"body {name}")
        self.make_bridge().pump_outbox()
        # Sorted by name, which for the bridge's stamped filenames is time order.
        self.assertEqual(self.client.texts, ["body a.md", "body b.md", "body c.md"])

    def test_nothing_is_sent_before_a_chat_is_bound(self):
        path = self.queue("m1.md", "queued up")
        bridge = self.make_bridge(pinned=None)

        self.assertEqual(bridge.pump_outbox(), 0)
        self.assertEqual(self.client.sent, [])
        self.assertTrue(path.exists(), "files must queue up, not vanish")

    def test_an_empty_message_is_archived_without_being_sent(self):
        self.queue("empty.md", "   ")
        bridge = self.make_bridge()
        bridge.pump_outbox()
        self.assertEqual(self.client.sent, [])
        self.assertTrue((self.layout.outbox_archive / "empty.md").is_file())

    def test_a_permanent_failure_quarantines_the_file_with_a_reason(self):
        path = self.queue("bad.md", "will never be accepted")
        client = FakeTelegramClient(fail_with=TelegramError("rejected", permanent=True))
        bridge = self.make_bridge(client=client)

        delivered = bridge.pump_outbox()

        self.assertEqual(delivered, 0)
        self.assertFalse(path.exists())
        self.assertFalse((self.layout.outbox_archive / "bad.md").exists())
        failed = self.layout.outbox_failed / "bad.md"
        self.assertTrue(failed.is_file())
        reason = self.layout.outbox_failed / "bad.md.reason"
        self.assertTrue(reason.is_file())
        self.assertIn("rejected", reason.read_text(encoding="utf-8"))

    def test_a_transient_failure_leaves_the_file_at_the_head_of_the_queue(self):
        path = self.queue("later.md", "try me again")
        client = FakeTelegramClient(fail_with=TelegramError("network blip", permanent=False))
        bridge = self.make_bridge(client=client)

        with self.assertRaises(TelegramError):
            bridge.pump_outbox()

        self.assertTrue(path.is_file(), "a transient failure must not lose the message")
        self.assertFalse((self.layout.outbox_archive / "later.md").exists())
        if self.layout.outbox_failed.exists():
            self.assertFalse((self.layout.outbox_failed / "later.md").exists())
        self.assertEqual(bridge.state.attempts.get("later.md"), 1)

    def test_a_transient_failure_stops_the_drain_to_preserve_ordering(self):
        for name in ("a.md", "b.md"):
            self.queue(name, f"body {name}")
        client = FakeTelegramClient(fail_with=TelegramError("blip", permanent=False))
        bridge = self.make_bridge(client=client)

        with self.assertRaises(TelegramError):
            bridge.pump_outbox()

        self.assertTrue((self.layout.outbox / "a.md").is_file())
        self.assertTrue((self.layout.outbox / "b.md").is_file())

    def test_a_transient_failure_is_quarantined_once_attempts_run_out(self):
        self.queue("stubborn.md", "keeps failing")
        client = FakeTelegramClient(fail_with=TelegramError("blip", permanent=False))
        bridge = self.make_bridge(client=client)

        for _ in range(bridge_module.MAX_SEND_ATTEMPTS - 1):
            with self.assertRaises(TelegramError):
                bridge.pump_outbox()
        bridge.pump_outbox()  # the attempt that exhausts the budget

        self.assertTrue((self.layout.outbox_failed / "stubborn.md").is_file())
        self.assertFalse((self.layout.outbox / "stubborn.md").exists())

    def test_an_unparseable_outbox_file_is_quarantined_not_retried_forever(self):
        path = self.layout.outbox / "broken.md"
        path.write_text("---\nproject: demo\nnever closed\n", encoding="utf-8")

        self.make_bridge().pump_outbox()

        self.assertFalse(path.exists())
        self.assertTrue((self.layout.outbox_failed / "broken.md").is_file())


class InboundCommandTests(BridgeTestCase):
    """``/c <project> <message>`` is the only inbound write path."""

    def setUp(self):
        super().setUp()
        self.collab_root = self.home / "demo"
        CollabPaths.at(self.collab_root, "demo").ensure()
        Registry(self.layout).register("demo", self.collab_root)
        self.bridge = self.make_bridge()

    def inbox_files(self, project: str):
        directory = self.layout.inbox_live / project
        return sorted(directory.glob("from-user-*.md")) if directory.is_dir() else []

    def test_a_message_for_a_known_project_is_written_to_its_live_inbox(self):
        handled = self.bridge._handle_message(
            telegram_message("/c demo please stop and look at the drain", chat_id=CHAT_ID)
        )

        self.assertTrue(handled)
        files = self.inbox_files("demo")
        self.assertEqual(len(files), 1)
        meta, body = frontmatter.parse_file(files[0])
        self.assertEqual(body.strip(), "please stop and look at the drain")
        self.assertEqual(meta["from"], "user")
        self.assertEqual(meta["to"], "builder")
        self.assertEqual(meta["project"], "demo")
        self.assertEqual(meta["source"], "telegram")
        self.assert_inside(files[0], self.layout.inbox_live)

    def test_a_multi_line_body_arrives_intact(self):
        body = "first line\nsecond line\n\nfourth line"
        self.bridge._handle_message(telegram_message(f"/c demo {body}", chat_id=CHAT_ID))
        _meta, written = frontmatter.parse_file(self.inbox_files("demo")[0])
        self.assertEqual(written.strip(), body)

    def test_two_messages_in_the_same_second_do_not_collide(self):
        for index in range(3):
            self.bridge._handle_message(
                telegram_message(f"/c demo message {index}", chat_id=CHAT_ID, message_id=index)
            )
        files = self.inbox_files("demo")
        self.assertEqual(len(files), 3)
        self.assertEqual(len({path.name for path in files}), 3)

    def test_a_traversal_project_name_creates_nothing_outside_inbox_live(self):
        before = {path for path in self.home.rglob("*")}

        handled = self.bridge._handle_message(
            telegram_message("/c ../../evil hi", chat_id=CHAT_ID)
        )

        self.assertTrue(handled, "the command is answered, not silently dropped")
        created = {path for path in self.home.rglob("*")} - before
        self.assertEqual(created, set(), "an unknown project must not be written at all")
        self.assertFalse((self.home.parent / "evil").exists())
        # The name would have been coerced into a single safe component anyway.
        coerced = slug.coerce_name("../../evil")
        self.assert_inside(self.layout.inbox_for(coerced), self.layout.inbox_live)

    def test_even_an_accepted_hostile_name_stays_inside_inbox_live(self):
        permissive = self.make_bridge(allow_unknown_projects=True)
        for raw in ("../../evil", "..\\..\\windows", "/etc/passwd", "C:\\Windows"):
            with self.subTest(raw=raw):
                permissive._handle_message(telegram_message(f"/c {raw} hi", chat_id=CHAT_ID))
        for path in self.layout.inbox_live.rglob("from-user-*.md"):
            self.assert_inside(path, self.layout.inbox_live)
        self.assertFalse((self.home.parent / "evil").exists())

    def test_an_unknown_project_is_refused_and_the_real_list_is_offered(self):
        self.bridge._handle_message(telegram_message("/c nosuch hello", chat_id=CHAT_ID))

        self.assertFalse((self.layout.inbox_live / "nosuch").exists())
        self.assertIn("demo", self.client.texts[-1])

    def test_a_project_name_that_reduces_to_nothing_is_refused(self):
        self.bridge._handle_message(telegram_message("/c ... hello", chat_id=CHAT_ID))
        self.assertEqual(self.inbox_files("demo"), [])

    def test_c_without_a_body_writes_nothing(self):
        self.bridge._handle_message(telegram_message("/c demo", chat_id=CHAT_ID))
        self.assertEqual(self.inbox_files("demo"), [])

    def test_c_without_a_project_answers_with_usage(self):
        self.bridge._handle_message(telegram_message("/c", chat_id=CHAT_ID))
        self.assertEqual(self.inbox_files("demo"), [])
        self.assertTrue(self.client.texts)

    def test_informational_commands_reply_without_writing_anything(self):
        for command in ("/help", "/start", "/whoami", "/projects", "/status", "/notacommand"):
            with self.subTest(command=command):
                self.client.sent.clear()
                handled = self.bridge._handle_message(
                    telegram_message(command, chat_id=CHAT_ID)
                )
                self.assertTrue(handled)
                self.assertTrue(self.client.sent)
        self.assertEqual(self.inbox_files("demo"), [])

    def test_a_command_addressed_to_the_bot_by_name_is_understood(self):
        self.bridge._handle_message(telegram_message("/help@somebot", chat_id=CHAT_ID))
        self.assertTrue(self.client.sent)


class ChatBindingTests(BridgeTestCase):
    """Learn-and-lock: adopt the first chat, ignore every other one."""

    def test_the_first_chat_is_adopted_and_the_rest_are_ignored(self):
        bridge = self.make_bridge(pinned=None)
        self.assertIsNone(bridge.state.chat_id)

        # The binding message binds but is not itself treated as a command.
        self.assertFalse(bridge._handle_message(telegram_message("/help", chat_id=111)))
        self.assertEqual(bridge.state.chat_id, 111)

        self.client.sent.clear()
        self.assertFalse(
            bridge._handle_message(telegram_message("/help", chat_id=222)),
            "a message from an unbound chat must be ignored",
        )
        self.assertEqual(self.client.sent, [], "a stranger must not get a reply")
        self.assertEqual(bridge.state.chat_id, 111, "the binding must not be stolen")

        self.assertTrue(bridge._handle_message(telegram_message("/help", chat_id=111)))
        self.assertTrue(self.client.sent)

    def test_a_stranger_cannot_write_into_a_project(self):
        CollabPaths.at(self.home / "demo", "demo").ensure()
        Registry(self.layout).register("demo", self.home / "demo")
        bridge = self.make_bridge(pinned=None)
        bridge._handle_message(telegram_message("/help", chat_id=111))  # binds

        bridge._handle_message(telegram_message("/c demo sneaky", chat_id=999))

        self.assertFalse((self.layout.inbox_live / "demo").exists())

    def test_a_pinned_chat_id_is_used_without_learning(self):
        bridge = self.make_bridge(pinned=CHAT_ID)
        self.assertEqual(bridge.state.chat_id, CHAT_ID)
        self.assertTrue(bridge.pinned)
        self.assertFalse(bridge._handle_message(telegram_message("/help", chat_id=777)))

    def test_a_non_integer_pinned_chat_id_is_rejected(self):
        with self.assertRaises(TelegramError):
            self.make_bridge(pinned="not-a-number")

    def test_the_binding_survives_a_restart(self):
        first = self.make_bridge(pinned=None)
        first._handle_message(telegram_message("/help", chat_id=111))
        second = bridge_module.Bridge(self.client, self.layout)
        self.assertEqual(second.state.chat_id, 111)


class UpdatePumpTests(BridgeTestCase):
    """The update offset is what stops a restart replaying an hour of chat."""

    def test_the_offset_advances_past_updates_that_were_ignored(self):
        client = FakeTelegramClient()
        client.updates = [
            {"update_id": 10, "message": telegram_message("/help", chat_id=999)},
            {"update_id": 11, "message": telegram_message("/help", chat_id=999)},
        ]
        bridge = self.make_bridge(client=client, pinned=CHAT_ID)

        bridge.pump_updates(long_poll=0)

        self.assertEqual(bridge.state.offset, 12)

    def test_the_offset_is_persisted(self):
        client = FakeTelegramClient()
        client.updates = [{"update_id": 42, "message": telegram_message("/help", chat_id=CHAT_ID)}]
        bridge = self.make_bridge(client=client, pinned=CHAT_ID)

        bridge.pump_updates(long_poll=0)

        saved = json.loads((self.layout.logs / "telegram-state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["offset"], 43)

    def test_updates_without_a_message_are_skipped(self):
        client = FakeTelegramClient()
        client.updates = [{"update_id": 1, "edited_message": {"text": "nope"}}]
        bridge = self.make_bridge(client=client, pinned=CHAT_ID)
        self.assertEqual(bridge.pump_updates(long_poll=0), 0)


class TokenRedactionTests(IsolatedHomeTestCase):
    """The token must never reach a log line, including inside a URL."""

    def test_redact_removes_the_token_from_arbitrary_text(self):
        client = bridge_module.TelegramClient("123456:SECRETVALUE")
        message = "https://api.telegram.org/bot123456:SECRETVALUE/getMe failed"
        redacted = client.redact(message)
        self.assertNotIn("SECRETVALUE", redacted)
        self.assertIn("<token>", redacted)

    def test_a_malformed_token_is_refused_at_construction(self):
        for token in ("", "no-colon-here"):
            with self.subTest(token=token):
                with self.assertRaises(TelegramError):
                    bridge_module.TelegramClient(token)
