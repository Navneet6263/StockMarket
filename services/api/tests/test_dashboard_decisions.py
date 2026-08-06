import unittest

from app.services.hot_picks import _dashboard_candidate_allowed, _dashboard_profile, map_pick


class DashboardDecisionTests(unittest.TestCase):
    def test_reentry_reversal_is_labeled_ready_and_can_pass_mtf_risk_screen(self):
        signal = {
            "symbol": "REVERSAL",
            "direction": "bullish",
            "action": "REENTRY_BUY",
            "allow_buy_call": True,
            "reversal_watch": True,
            "riskPct": 2.0,
            "risk_reward": 2.4,
            "relative_volume": 2.1,
            "large_money_footprint": {"score": 35},
        }

        result = _dashboard_profile(signal, score=82)

        self.assertEqual(result["dashboardBucket"], "REVERSAL_READY")
        self.assertTrue(result["dashboardEligible"])
        self.assertTrue(result["mtfRiskFit"])

    def test_fast_move_with_valid_tight_plan_is_momentum_not_blanket_chase(self):
        signal = {
            "symbol": "MOMENTUM",
            "direction": "bullish",
            "action": "WATCH",
            "chase_risk": True,
            "is_valid_entry_after_move": True,
            "riskPct": 2.5,
            "risk_reward": 1.8,
            "relative_volume": 3.0,
            "change_pct": 7.0,
        }

        result = _dashboard_profile(signal, score=80)

        self.assertEqual(result["dashboardBucket"], "MOMENTUM_READY")
        self.assertTrue(result["dashboardEligible"])

    def test_fast_move_without_valid_plan_is_retest_only(self):
        signal = {
            "symbol": "CHASE",
            "direction": "bullish",
            "action": "BUY",
            "chase_risk": True,
            "is_valid_entry_after_move": False,
            "riskPct": 8.0,
            "risk_reward": 1.5,
            "relative_volume": 5.0,
        }

        result = _dashboard_profile(signal, score=88)

        self.assertEqual(result["dashboardBucket"], "WAIT_RETEST")
        self.assertFalse(result["dashboardEligible"])
        self.assertFalse(result["mtfRiskFit"])

    def test_sixty_session_extension_requires_a_new_base(self):
        signal = {
            "symbol": "EXTENDED",
            "direction": "bullish",
            "action": "BUY",
            "return_20d": 18.0,
            "return_60d": 60.0,
            "riskPct": 2.0,
            "risk_reward": 2.5,
            "relative_volume": 2.0,
        }

        result = _dashboard_profile(signal, score=90)

        self.assertEqual(result["dashboardBucket"], "LONG_TERM_EXTENDED")
        self.assertFalse(result["dashboardEligible"])

    def test_high_trap_never_becomes_reversal_ready(self):
        signal = {
            "symbol": "TRAP",
            "direction": "bullish",
            "action": "REENTRY_BUY",
            "reversal_watch": True,
            "riskPct": 2.0,
            "risk_reward": 3.0,
            "relative_volume": 4.0,
            "demand_supply": {"trapRisk": "high"},
        }

        result = _dashboard_profile(signal, score=90)

        self.assertEqual(result["dashboardBucket"], "NO_TRADE")
        self.assertFalse(result["dashboardEligible"])

    def test_strict_reversal_watch_remains_visible_for_live_confirmation(self):
        item = {
            "symbol": "GAEL",
            "score": 75,
            "dashboardEligible": True,
            "freshEntryAllowed": False,
            "raw": {
                "attention_only": True,
                "requires_live_confirmation": True,
            },
        }

        self.assertTrue(_dashboard_candidate_allowed(item, set()))

        item["raw"]["requires_live_confirmation"] = False
        self.assertFalse(_dashboard_candidate_allowed(item, set()))

    def test_mapped_pick_exposes_source_action_instead_of_defaulting_to_watch(self):
        pick = map_pick(
            {
                "symbol": "READY",
                "direction": "bullish",
                "action": "BUY",
                "allow_buy_call": True,
                "current_price": 100,
                "safe_entry_price": 101,
                "stop_loss": 98,
                "target_1": 108,
                "riskPct": 2.0,
                "risk_reward": 2.5,
                "relative_volume": 2.0,
                "large_money_footprint": {"score": 30},
            },
            "2026-08-06T09:30:00+00:00",
        )

        self.assertEqual(pick["sourceAction"], "BUY")
        self.assertEqual(pick["dashboardBucket"], "SETUP_READY")
        self.assertEqual(pick["dashboardAction"], "Setup ready")


if __name__ == "__main__":
    unittest.main()
