import unittest

import main


class FakeTrack:
    def __init__(self, identifier):
        self.identifier = identifier
        self.title = identifier


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

    async def play(self, track):
        self.played.append(track)
        if self.fail_first and len(self.played) == 1:
            raise RuntimeError("test failure")
        self.current = track


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main.bot.player_last_update.clear()
        main.bot.player_unhealthy_since.clear()
        main.bot.player_recovery_locks.clear()

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
