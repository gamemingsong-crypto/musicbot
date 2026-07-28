import unittest

import main


class FakeTrack:
    def __init__(self, identifier, *, length=180_000, is_stream=False):
        self.identifier = identifier
        self.title = identifier
        self.length = length
        self.is_stream = is_stream


class FakeQueue:
    def __init__(self, *tracks):
        self.tracks = list(tracks)

    @property
    def is_empty(self):
        return not self.tracks

    async def get_wait(self):
        return self.tracks.pop(0)


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id


class FakeChannel:
    name = "Music Room Test"


class FakePlayer:
    def __init__(self, guild_id, current, *queued, fail_first=False):
        self.guild = FakeGuild(guild_id)
        self.channel = FakeChannel()
        self.current = current
        self.queue = FakeQueue(*queued)
        self.fail_first = fail_first
        self.played = []
        self.play_calls = []

    async def play(self, track, **kwargs):
        self.played.append(track)
        self.play_calls.append((track, kwargs))
        if self.fail_first and len(self.played) == 1:
            raise RuntimeError("test failure")
        self.current = track


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.bot.player_last_update.clear()
        main.bot.player_unhealthy_since.clear()
        main.bot.player_recovery_locks.clear()
        main.bot.player_resume_positions.clear()
        main.bot.player_transient_retries.clear()
        main.bot.voice_connect_locks.clear()
        main.bot.node_reconnect_locks.clear()

    async def test_replay_current_does_not_consume_queue(self):
        current = FakeTrack("current")
        queued_one = FakeTrack("queued-1")
        queued_two = FakeTrack("queued-2")
        player = FakePlayer(1, current, queued_one, queued_two)

        recovered = await main.recover_player(
            player,
            reason="test",
            replay_current=True,
        )

        self.assertTrue(recovered)
        self.assertEqual(player.played, [current])
        self.assertEqual(player.queue.tracks, [queued_one, queued_two])
        self.assertEqual(player.play_calls[0][1]["start"], 0)
        self.assertFalse(player.play_calls[0][1]["add_history"])

    async def test_failed_replay_advances_only_one_queue_item(self):
        current = FakeTrack("current")
        queued_one = FakeTrack("queued-1")
        queued_two = FakeTrack("queued-2")
        player = FakePlayer(2, current, queued_one, queued_two, fail_first=True)

        recovered = await main.recover_player(
            player,
            reason="test",
            replay_current=True,
        )

        self.assertTrue(recovered)
        self.assertEqual(player.played, [current, queued_one])
        self.assertEqual(player.queue.tracks, [queued_two])
        self.assertEqual(player.play_calls[1][1]["start"], 0)
        self.assertTrue(player.play_calls[1][1]["add_history"])

    async def test_replay_current_resumes_from_last_checkpoint(self):
        current = FakeTrack("current")
        player = FakePlayer(3, current)
        main.remember_player_position(player, 60_000)

        recovered = await main.recover_player(
            player,
            reason="network interruption",
            replay_current=True,
        )

        self.assertTrue(recovered)
        self.assertEqual(player.play_calls[0][1]["start"], 57_000)
        self.assertFalse(player.play_calls[0][1]["add_history"])

    async def test_live_stream_recovery_starts_from_zero(self):
        current = FakeTrack("live", is_stream=True)
        player = FakePlayer(4, current)
        main.remember_player_position(player, 60_000)

        recovered = await main.recover_player(
            player,
            reason="network interruption",
            replay_current=True,
        )

        self.assertTrue(recovered)
        self.assertEqual(player.play_calls[0][1]["start"], 0)

    def test_dns_failure_is_treated_as_transient(self):
        exception = {
            "message": "All clients failed",
            "cause": "java.net.UnknownHostException: Temporary failure in name resolution",
        }

        self.assertTrue(main.is_transient_track_error(exception))
        self.assertFalse(main.is_transient_track_error({"message": "Video unavailable"}))

    def test_transient_retries_are_bounded(self):
        track = FakeTrack("current")
        player = FakePlayer(5, track)

        attempts = []
        for _ in range(main.PLAYER_TRANSIENT_RETRY_MAX + 1):
            attempts.append(main.register_transient_track_retry(player, track))
            main.release_transient_retry(player, track)

        self.assertEqual(attempts[:-1], list(range(1, main.PLAYER_TRANSIENT_RETRY_MAX + 1)))
        self.assertIsNone(attempts[-1])

    def test_duplicate_transient_event_uses_pending_retry(self):
        track = FakeTrack("current")
        player = FakePlayer(6, track)

        self.assertEqual(main.register_transient_track_retry(player, track), 1)
        self.assertEqual(main.register_transient_track_retry(player, track), 0)

    def test_voice_connect_timeout_detection(self):
        channel_timeout = type("ChannelTimeoutException", (Exception,), {})

        self.assertTrue(main.is_voice_connect_timeout(channel_timeout()))
        self.assertTrue(main.is_voice_connect_timeout(TimeoutError()))
        self.assertFalse(main.is_voice_connect_timeout(RuntimeError()))

    async def test_dead_wavelink_websocket_is_reconnected(self):
        class FakeTask:
            def __init__(self, done):
                self._done = done

            def done(self):
                return self._done

            def cancelled(self):
                return False

            def exception(self):
                return TypeError("bad websocket frame")

        class FakeWebsocket:
            def __init__(self):
                self.keep_alive_task = FakeTask(True)
                self.connect_calls = 0

            async def connect(self):
                self.connect_calls += 1
                self.keep_alive_task = FakeTask(False)

        class FakeNode:
            identifier = "test-node"

            def __init__(self):
                self._websocket = FakeWebsocket()

        node = FakeNode()
        nodes = main.wavelink.Pool._Pool__nodes
        original_nodes = nodes.copy()
        nodes.clear()
        nodes[node.identifier] = node
        try:
            await main.check_wavelink_websocket_health()
        finally:
            nodes.clear()
            nodes.update(original_nodes)

        self.assertEqual(node._websocket.connect_calls, 1)

    async def test_missing_loading_message_does_not_abort_command(self):
        class FakeResponse:
            status = 404
            reason = "Not Found"

        class MissingMessage:
            async def delete(self, *, delay=None):
                raise main.discord.NotFound(
                    FakeResponse(),
                    {"code": 10008, "message": "Unknown Message"},
                )

        await main.safe_delete_message(MissingMessage())

    def test_failed_track_end_is_ignored_once(self):
        player = object.__new__(FakePlayer)
        track = FakeTrack("failed")
        main.mark_failed_track_recovery(player, track)

        self.assertTrue(main.should_ignore_failed_track_end(player, track))
        self.assertFalse(
            main.should_ignore_failed_track_end(player, FakeTrack("different"))
        )


if __name__ == "__main__":
    unittest.main()
