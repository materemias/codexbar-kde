"""Run the notification transition policy in the same Qt JS engine as Plasma."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
QML = shutil.which("qml6") or shutil.which("qml") or "/usr/lib/qt6/bin/qml"


@unittest.skipUnless(Path(QML).is_file(), "Install the Qt 6 qml tool first")
class NotificationPolicyTest(unittest.TestCase):
    def policy(self, scenario):
        with tempfile.TemporaryDirectory(prefix="codexbar-notifications-") as directory:
            root = Path(directory)
            for name in ("Notifications.js", "Usage.js"):
                shutil.copy2(ROOT / "contents/ui" / name, root / name)
            (root / "test.qml").write_text('''import QtQuick
import "Notifications.js" as Notifications
import "Usage.js" as Usage
Item {
    function check(condition, message) {
        if (!condition) throw new Error(message)
    }
    function record(percent, email, reset) {
        return { id: "codex", ok: true, accountEmail: email || null,
            sourceScope: "a".repeat(64),
            primary: { usedPercent: percent, resetsAt: reset || null } }
    }
    function mode(effective, operating, stalled) {
        return { mode: effective, operatingMode: operating || "auto",
            stalled: stalled === undefined ? false : stalled,
            accountAvailability: { "private@example.invalid": "blocked" },
            description: "private description" }
    }
    function delivered(result) {
        var state = result.state
        for (var i = 0; i < result.events.length; i++)
            state = Notifications.delivery(state, result.events[i], true)
        return state
    }
    Component.onCompleted: {
        try {
            var now = Date.parse("2026-09-29T12:00:00Z")
''' + scenario + '''
            console.log("PASS notification policy")
            Qt.exit(0)
        } catch (error) {
            console.error(error.message)
            Qt.exit(1)
        }
    }
}
''')
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                       QT_FORCE_STDERR_LOGGING="1", QT_LOGGING_TO_CONSOLE="1")
            result = subprocess.run([QML, str(root / "test.qml")], env=env,
                                    capture_output=True, text=True, timeout=15)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("PASS notification policy", output)

    def test_thresholds_escalate_once_and_resets_rearm(self):
        self.policy('''
            var reset = "2026-09-29T15:00:00Z"
            var result = Notifications.usage({}, [record(79, null, reset)], now, true)
            check(result.events.length === 0, "79% must stay quiet without a projection")
            result = Notifications.usage(result.state, [record(80, null, reset)], now, true)
            check(result.events.length === 1 && result.events[0].urgency === "normal",
                "80% must warn")
            var warned = delivered(result)
            result = Notifications.usage(warned, [record(94, null, reset)], now, true)
            check(result.events.length === 0, "polling in the warning range must not repeat")
            result = Notifications.usage(result.state, [record(95, null, reset)], now, true)
            check(result.events.length === 1 && result.events[0].urgency === "critical",
                "95% must escalate")
            var critical = delivered(result)
            result = Notifications.usage(critical, [record(98, null, reset)], now, true)
            check(result.events.length === 0, "critical poll must not repeat")
            result = Notifications.usage(result.state, [record(82, null, reset)], now, true)
            check(result.events.length === 0, "a downward correction must not rearm")
            result = Notifications.usage(result.state,
                [record(80, null, "2026-09-29T20:00:00Z")], now, true)
            check(result.events.length === 1, "a new reset period must rearm")
            check(Notifications.usage(critical, [record(98, null, reset)], now, true)
                .events.length === 0, "reducer must not mutate earlier state")
        ''')

    def test_projection_uses_shared_pace_with_three_percent_floor(self):
        self.policy('''
            var item = record(4)
            item.primary.windowMinutes = 300
            item.primary.resetsAt = new Date(now + 300 * 60000 * 0.98).toISOString()
            check(Usage.projectedPercent(item.primary, now) === -1,
                "projection must be suppressed before 3% elapsed")
            var result = Notifications.usage({}, [item], now, true)
            check(result.events.length === 0, "early pace noise must stay quiet")
            item.primary.resetsAt = new Date(now + 300 * 60000 * 0.97).toISOString()
            result = Notifications.usage(result.state, [item], now, true)
            check(result.events.length === 1 && result.events[0].body.indexOf("Projected") >= 0,
                "settled over-pace usage must warn")
            result = Notifications.usage(result.state, [item], now, true)
            check(result.events.length === 0, "projected exhaustion must not repeat")
            item.primary.usedPercent = 80
            result = Notifications.usage(result.state, [item], now, true)
            check(result.events.length === 1 && result.events[0].body.indexOf("80%") >= 0,
                "actual 80% usage must escalate beyond a projection")
            item.primary.startsAt = new Date(now - 60 * 60000).toISOString()
            item.primary.resetsAt = new Date(now + 60 * 60000).toISOString()
            item.primary.usedPercent = 60
            check(Usage.pacePercent(item.primary, now) === 50
                && Usage.projectedPercent(item.primary, now) === 120,
                "weekly-truncated windows must use their real span")
            result = Notifications.usage({}, [item], now, true)
            check(result.events.length === 1, "truncated-window projection must alert")
            item.primary.usedPercent = 50
            check(Notifications.usage({}, [item], now, true).events.length === 0,
                "projecting exactly 100% is not exhaustion before reset")
        ''')

    def test_failed_stale_disabled_and_composite_readings_do_not_alert(self):
        self.policy('''
            var stale = record(99); stale.stale = true
            var failed = record(99); failed.ok = false
            var composite = record(99); composite.composite = true
            var expired = record(99, null, "2026-09-29T11:00:00Z")
            var invalid = record(NaN)
            var malformed = record(99, null, "not a date")
            for (var item of [stale, failed, composite, expired, invalid, malformed])
                check(Notifications.usage({}, [item], now, true).events.length === 0,
                    "unreliable readings must not alert")
            check(Notifications.usage({}, [record(99)], now, false).events.length === 0,
                "disabled notifications must stay quiet")
            var state = Notifications.usage({}, [record(80)], now, true).state
            state = Notifications.usage(state, [stale], now, true).state
            check(Notifications.usage(state, [record(81)], now, true).events.length === 0,
                "stale intervals must preserve deduplication")
            check(Notifications.usage({}, [stale], now, true).events.length === 0
                && Notifications.usage({}, [record(99)], now, true).events.length === 1,
                "startup cache stays silent but first fresh reading can alert")
        ''')

    def test_accounts_windows_and_private_notification_text(self):
        self.policy('''
            var a = record(80, "private-a@example.invalid")
            var b = record(80, "private-b@example.invalid")
            a.secondary = { usedPercent: 95 }
            a.extraRateWindows = [{ id: "grant", title: "private-a@example.invalid",
                window: { usedPercent: 80 } }]
            var result = Notifications.usage({}, [a, b], now, true)
            check(result.events.length === 4, "accounts and windows must alert independently")
            var text = JSON.stringify(result.events.map(function(event) {
                return [event.title, event.body]
            }))
            check(text.indexOf("private-") < 0 && text.indexOf("account 1") >= 0
                && text.indexOf("account 2") >= 0, "notification text must use benign account labels")
            check(Notifications.usage(result.state, [b, a], now, true).events.length === 0,
                "account reorder must not repeat alerts")
            check(Notifications.usage({}, [record(99), record(99)], now, true).events.length === 0,
                "ambiguous unnamed accounts must not get fabricated identities")
            var anonymous = record(99)
            result = Notifications.usage({}, [anonymous, a], now, true)
            check(result.events.length === 3, "known accounts still work beside an ambiguous account")
            result = Notifications.usage({}, [a, a], now, true)
            check(result.events.length === 3, "duplicate records must not duplicate alerts")
        ''')

    def test_failed_delivery_retries_without_repeating_successes(self):
        self.policy('''
            var a = record(95, "private-a@example.invalid")
            var b = record(95, "private-b@example.invalid")
            var result = Notifications.usage({}, [a, b], now, true)
            check(Notifications.usage(result.state, [a, b], now, true).events.length === 0,
                "in-flight delivery must suppress duplicate polls")
            var state = Notifications.delivery(result.state, result.events[0], true)
            state = Notifications.delivery(state, result.events[1], false)
            var retry = Notifications.usage(state, [a, b], now, true)
            check(retry.events.length === 1 && retry.events[0].usageKey === result.events[1].usageKey,
                "retry only the failed account, not a successful delivery")
            check(Notifications.usage(delivered(retry), [a, b], now, true).events.length === 0,
                "a successful retry must settle the warning")
            var warning = Notifications.usage({}, [record(80)], now, true)
            var critical = Notifications.usage(warning.state, [record(95)], now, true)
            state = Notifications.delivery(critical.state, warning.events[0], false)
            state = Notifications.delivery(state, critical.events[0], false)
            check(Notifications.usage(state, [record(95)], now, true).events.length === 1,
                "failed overlapping escalation must not consume either level")
            state = Notifications.delivery(critical.state, critical.events[0], true)
            state = Notifications.delivery(state, warning.events[0], false)
            check(Notifications.usage(state, [record(95)], now, true).events.length === 0,
                "late lower-level failure must not erase a successful escalation")
            var reset = "2026-09-29T15:00:00Z"
            var earlier = Notifications.usage({}, [record(80, null, reset)], now, true)
            var later = Notifications.usage(earlier.state,
                [record(80, null, "2026-09-29T20:00:00Z")], now, true)
            state = Notifications.delivery(later.state, earlier.events[0], false)
            check(Notifications.usage(state,
                [record(80, null, "2026-09-29T20:00:00Z")], now, true).events.length === 0,
                "an old period completion must not disturb current pending delivery")
        ''')

    def test_credential_scope_changes_rearm_anonymous_accounts_and_retire_deliveries(self):
        self.policy('''
            var a = record(80, null, "2026-09-29T15:00:00Z")
            var first = Notifications.usage({}, [a], now, true)
            var b = record(80, null, "2026-09-29T15:00:00Z")
            b.sourceScope = "b".repeat(64)
            var next = Notifications.usage(first.state, [b], now, true)
            check(next.events.length === 1, "new credentials must have an independent allowance alert")
            check(!next.state[first.events[0].usageKey], "retired scope must lose pending deliveries")
            var state = Notifications.delivery(next.state, first.events[0], true)
            state = Notifications.delivery(state, next.events[0], false)
            var retry = Notifications.usage(state, [b], now, true)
            check(retry.events.length === 1, "late old-scope success must not consume a new-scope retry")
            check(JSON.stringify(next.events.map(function(e) { return [e.title, e.body] }))
                .indexOf(b.sourceScope) < 0, "credential scope is never notification text")
            var returned = Notifications.usage(next.state, [a], now, true)
            state = Notifications.delivery(returned.state, first.events[0], true)
            state = Notifications.delivery(state, returned.events[0], false)
            check(Notifications.usage(state, [a], now, true).events.length === 1,
                "returning to retired credentials cannot revive their old pending callback")
            delete b.sourceScope
            var unknown = Notifications.usage(retry.state, [b], now, true)
            check(unknown.events.length === 0 && Object.keys(unknown.state).length === 0,
                "unknown credentials must not alert or retain acknowledged identities")
        ''')

    def test_resetless_crossing_rearms_without_guessing_periods_and_ignores_old_delivery(self):
        self.policy('''
            var high = record(80)
            high.primary.windowMinutes = 300
            var first = Notifications.usage({}, [high], now, true)
            var state = delivered(first)
            check(Notifications.usage(state, [high], now + 366 * 86400000, true).events.length === 0,
                "elapsed time alone must not invent an unknown reset")
            var low = record(79)
            low.stale = true
            state = Notifications.usage(state, [low], now, true).state
            check(Notifications.usage(state, [high], now, true).events.length === 0,
                "stale usage cannot establish an observed reset")
            low.stale = false
            state = Notifications.usage(first.state, [low], now, true).state
            var next = Notifications.usage(state, [high], now, true)
            check(next.events.length === 1, "fresh below-80 reading must rearm the next crossing")
            state = Notifications.delivery(next.state, first.events[0], true)
            state = Notifications.delivery(state, next.events[0], false)
            check(Notifications.usage(state, [high], now, true).events.length === 1,
                "old observed-cycle delivery cannot consume the new crossing")
            var known = record(80, null, "2026-09-29T15:00:00Z")
            state = delivered(Notifications.usage({}, [known], now, true))
            known.primary.usedPercent = 10
            state = Notifications.usage(state, [known], now, true).state
            known.primary.usedPercent = 80
            check(Notifications.usage(state, [known], now, true).events.length === 0,
                "known reset periods must not rearm on downward corrections")
        ''')

    def test_primary_and_reserve_weekly_alerts_identify_distinct_windows(self):
        self.policy('''
            var item = record(80)
            item.primary.windowMinutes = 10080
            item.extraRateWindows = [
                { id: "gpt-reserve", window: { usedPercent: 80, windowMinutes: 10080 } },
                { id: "private-slot", title: "private@example.invalid", window: { usedPercent: 80 } }]
            var result = Notifications.usage({}, [item], now, true)
            check(result.events.length === 3, "each independent allowance must alert")
            check(result.events[0].body !== result.events[1].body,
                "primary weekly and reserve weekly alerts must be distinguishable")
            check(JSON.stringify(result.events).indexOf("private@example.invalid") < 0
                && result.events[2].body.indexOf("private-slot") < 0,
                "unrecognized window labels must not leak provider-supplied identity")
        ''')

    def test_openrouter_monthly_allowance_rearms_without_reset_timestamp(self):
        self.policy('''
            var item = { id: "openrouter", ok: true, sourceScope: "a".repeat(64),
                primary: { usedPercent: 95 } }
            var september = Date.parse("2026-09-30T23:59:59Z")
            var october = Date.parse("2026-10-01T00:00:00Z")
            var result = Notifications.usage({}, [item], september, true)
            var state = delivered(result)
            check(Notifications.usage(state, [item], september, true).events.length === 0,
                "same UTC month must deduplicate")
            item.primary.usedPercent = 0
            result = Notifications.usage(state, [item], october, true)
            check(result.events.length === 0, "month rollover itself must not alert")
            item.primary.usedPercent = 80
            result = Notifications.usage(result.state, [item], october + 86400000, true)
            check(result.events.length === 1 && result.events[0].urgency === "normal",
                "next month's 80% crossing must rearm")
            state = delivered(result)
            item.primary.usedPercent = 95
            check(Notifications.usage(state, [item], october + 86400000, true).events.length === 1,
                "the new month must allow critical escalation")
        ''')

    def test_omp_mode_initial_same_and_effective_operating_stalled_changes(self):
        self.policy('''
            var result = Notifications.ompMode(null, mode("FILL"), true)
            check(result.events.length === 0, "initial status must stay silent")
            result = Notifications.ompMode(result.state, mode("FILL"), true)
            check(result.events.length === 0, "same status must stay silent")
            result = Notifications.ompMode(result.state, mode("TARGET"), true)
            check(result.events.length === 1
                && result.events[0].body === "auto · FILL → auto · TARGET",
                "effective-mode transition must name from and to")
            var earlier = result.state
            result = Notifications.ompMode(result.state, mode("TARGET", "confirm"), true)
            check(result.events.length === 1
                && result.events[0].body === "auto · TARGET → confirm · TARGET",
                "operating-mode change must alert once")
            result = Notifications.ompMode(result.state, mode("TARGET", "confirm", true), true)
            check(result.events.length === 1
                && result.events[0].body === "confirm · TARGET → confirm · STALLED (TARGET)",
                "stalled transition must keep the underlying mode")
            result = Notifications.ompMode(result.state, mode("BURN", "confirm", true), true)
            check(result.events.length === 1
                && result.events[0].body === "confirm · STALLED (TARGET) → confirm · STALLED (BURN)",
                "underlying mode changes while stalled must remain visible")
            result = Notifications.ompMode(result.state, mode("BURN", "confirm"), true)
            check(result.events.length === 1
                && result.events[0].body === "confirm · STALLED (BURN) → confirm · BURN",
                "resuming from stalled must alert")
            check(JSON.stringify(result.events).indexOf("private") < 0,
                "events must not contain account or arbitrary status data")
            check(earlier.mode === "auto · TARGET",
                "transitions must not mutate earlier observations")
        ''')

    def test_omp_mode_unavailable_invalid_disabled_and_reenabled_baselines(self):
        self.policy('''
            var valid = Notifications.ompMode(null, mode("FILL"), true)
            for (var invalid of [null, {}, mode("unknown"), mode("FILL", "unknown"),
                    mode("FILL", "auto", "true"), { mode: "FILL", operatingMode: "auto" }]) {
                var result = Notifications.ompMode(valid.state, invalid, true)
                check(result.events.length === 0 && result.state.mode === null,
                    "unavailable or invalid status must clear the comparison baseline")
                result = Notifications.ompMode(result.state, mode("TARGET"), true)
                check(result.events.length === 0, "returning after a gap must be silent")
                result = Notifications.ompMode(result.state, mode("IDLE"), true)
                check(result.events.length === 1, "next available transition must alert")
            }
            result = Notifications.ompMode(valid.state, mode("TARGET"), false)
            check(result.events.length === 0, "disabled mode alerts must stay quiet")
            result = Notifications.ompMode(result.state, mode("IDLE"), true)
            check(result.events.length === 0, "enabling establishes a silent baseline")
            result = Notifications.ompMode(result.state, mode("NO-BANK"), true)
            check(result.events.length === 1, "next transition after enabling must alert")
            result = Notifications.ompMode(result.state, mode("EXPIRY-BURN"), true)
            check(result.events.length === 1, "every supported effective mode is valid")
        ''')

    def test_omp_mode_delivery_retries_only_current_transition(self):
        self.policy('''
            var initial = Notifications.ompMode(null, mode("FILL"), true)
            var changed = Notifications.ompMode(initial.state, mode("TARGET"), true)
            var event = changed.events[0]
            var result = Notifications.ompMode(changed.state, mode("TARGET"), true)
            check(result.events.length === 0, "inflight sends must not repeat each poll")
            var failed = Notifications.ompModeDelivery(result.state, event, false)
            result = Notifications.ompMode(failed, mode("TARGET"), true)
            check(result.events.length === 1 && result.events[0].body === event.body,
                "a failed delivery must retry on the next matching fresh reading")
            var delivered = Notifications.ompModeDelivery(result.state, result.events[0], true)
            check(Notifications.ompMode(delivered, mode("TARGET"), true).events.length === 0,
                "successful delivery must not repeat")
            var newer = Notifications.ompMode(changed.state, mode("IDLE"), true)
            var state = Notifications.ompModeDelivery(newer.state, event, true)
            check(state.transition.sequence === newer.events[0].sequence && state.pending,
                "old acknowledgement must not settle a newer transition")
            var gap = Notifications.ompMode(failed, null, true)
            var baseline = Notifications.ompMode(gap.state, mode("FILL"), true)
            newer = Notifications.ompMode(baseline.state, mode("TARGET"), true)
            state = Notifications.ompModeDelivery(newer.state, event, false)
            check(state.pending && state.transition.sequence !== event.sequence,
                "old acknowledgement must not reopen a post-gap transition")
            check(Notifications.ompMode(gap.state, mode("TARGET"), true).events.length === 0,
                "unavailable gap must discard a failed pending event")
        ''')

    def test_agent_transitions_have_silent_startup_and_enable_baselines(self):
        self.policy('''
            function session(id, state) {
                return { provider: "codex", sessionId: id, state: state,
                    lastPrompt: "private prompt", cwd: "/private/project" }
            }
            var result = Notifications.agents(null, [session("known", "blocked")], true)
            check(result.events.length === 0, "startup blocked baseline must be silent")
            result = Notifications.agents(result.state, [session("known", "working")], true)
            result = Notifications.agents(result.state,
                [session("known", "blocked"), session("new", "blocked")], true)
            check(result.events.length === 1, "only an already-known session transition alerts")
            check(JSON.stringify(result.events).indexOf("private") < 0,
                "agent notifications must not expose prompts or paths")
            result = Notifications.agents(result.state, [session("known", "blocked")], true)
            check(result.events.length === 0, "blocked polls must not repeat")
            result = Notifications.agents(result.state, [session("known", "working")], false)
            result = Notifications.agents(result.state, [session("known", "blocked")], true)
            check(result.events.length === 0, "enabling must establish a silent baseline")
            result = Notifications.agents(result.state, [session("known", "working")], true)
            result = Notifications.agents(result.state, [session("known", "blocked")], true)
            check(result.events.length === 1, "next genuine transition must rearm")
            result = Notifications.agents(result.state, [], true)
            result = Notifications.agents(result.state, [session("known", "blocked")], true)
            check(result.events.length === 0, "returning after absence establishes a new baseline")
            result = Notifications.agents(null,
                [session("", "working"), session("untracked-codex-42", "working")], true)
            result = Notifications.agents(result.state,
                [session("", "blocked"), session("untracked-codex-42", "blocked")], true)
            check(result.events.length === 0, "unknown session identities must not alert")
        ''')


if __name__ == "__main__":
    unittest.main()
