from support import LiveIntegrationTestCase, requires_live_integration


@requires_live_integration
class OperatorIntegrationTests(LiveIntegrationTestCase):
    def test_public_surface(self):
        runner = self.new_runner()
        runner.run_public_smoke()

        unexpected_errors = [
            outcome.detail
            for outcome in runner.public_outcomes
            if outcome.outcome == "unexpected_error"
        ]

        self.assertEqual(len(runner.public_outcomes), runner.expected_public_outcomes)
        self.assertFalse(unexpected_errors, unexpected_errors)
