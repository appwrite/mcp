from support import LiveIntegrationTestCase, requires_live_integration


@requires_live_integration
class AvatarsIntegrationTests(LiveIntegrationTestCase):
    def test_avatars_smoke(self):
        runner = self.new_runner()

        runner.call("avatars_get_browser", {"code": "ch"})
        runner.call("avatars_get_initials", {"name": "MCP Smoke"})
        runner.call("avatars_get_photo", {"name": "MCP Smoke"})

        self.assert_no_unexpected_errors(runner)
