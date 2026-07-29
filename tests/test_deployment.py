from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_lavalink_is_loopback_only_and_has_no_literal_password(self):
        config = (ROOT / "deploy" / "application.yml").read_text(encoding="utf-8")

        self.assertIn("address: 127.0.0.1", config)
        self.assertIn('password: "${LAVALINK_PASSWORD}"', config)
        self.assertNotIn("youshallnotpass", config)

    def test_optional_plugins_are_pinned(self):
        config = (ROOT / "deploy" / "application.yml").read_text(encoding="utf-8")

        self.assertIn("sponsorblock-plugin:3.0.1", config)
        self.assertIn("java-lyrics-plugin:1.6.6", config)

    def test_oauth_has_a_compatible_youtube_client(self):
        config = (ROOT / "deploy" / "application.yml").read_text(encoding="utf-8")

        self.assertIn("oauth:\n      enabled: true", config)
        self.assertIn("      - TV\n", config)

    def test_launchers_forward_pm2_signals(self):
        music_launcher = (ROOT / "deploy" / "start-musicbot.sh").read_text(encoding="utf-8")
        lavalink_launcher = (ROOT / "deploy" / "start-lavalink.sh").read_text(encoding="utf-8")

        self.assertIn("exec python3", music_launcher)
        self.assertIn("exec java", lavalink_launcher)


if __name__ == "__main__":
    unittest.main()
