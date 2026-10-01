.pragma library
.import "Usage.js" as Usage

function providerName(id) {
    var names = { claude: "Claude", codex: "Codex", zai: "z.ai",
        opencodego: "OpenCode Go", openrouter: "OpenRouter", kilo: "Kilo",
        typesafe: "TypeSafe", omp: "omp", pi: "Pi", opencode: "OpenCode" }
    return names[id] || "Coding agent"
}

function accountIdentity(record) {
    return typeof record.accountEmail === "string" && record.accountEmail.trim()
        ? record.accountEmail.trim().toLowerCase() : null
}

// A projection below this much actual use is early-window noise: the first
// prompt of a fresh 5h window can project far past 100%.
var PROJECTION_MIN_PERCENT = 10

function usage(previous, records, now, enabled) {
    var state = Object.assign({}, previous || {})
    var events = []
    var scopes = {}
    var counts = {}
    for (var a = 0; a < records.length; a++) {
        var account = records[a]
        if (!account || typeof account.id !== "string" || account.composite) continue
        if (typeof account.sourceScope === "string" && /^[a-f0-9]{64}$/.test(account.sourceScope))
            scopes[JSON.stringify([account.id, account.sourceScope])] = true
        var providerKey = JSON.stringify(account.id)
        counts[providerKey] = (counts[providerKey] || 0) + 1
    }
    for (var priorKey in state)
        if (!scopes[state[priorKey].source]) delete state[priorKey]
    if (!enabled) return { state: state, events: events }
    for (var p = 0; p < records.length; p++) {
        var record = records[p]
        if (!record || record.ok !== true || record.stale === true || record.composite
                || typeof record.id !== "string") continue
        var source = JSON.stringify([record.id, record.sourceScope])
        if (!scopes[source]) continue
        var provider = JSON.stringify(record.id)
        var accountId = accountIdentity(record)
        // Without an identity, two accounts cannot be safely distinguished.
        if (accountId === null && counts[provider] > 1) continue
        var label = providerName(record.id) + (accountId !== null ? " · " + accountId : "")
        var windows = []
        var slots = ["primary", "secondary", "tertiary"]
        for (var s = 0; s < slots.length; s++)
            windows.push({ slot: slots[s], window: record[slots[s]] })
        var extras = Array.isArray(record.extraRateWindows) ? record.extraRateWindows : []
        for (var e = 0; e < extras.length; e++) {
            var extra = extras[e]
            if (extra && typeof extra.id === "string" && extra.id.length > 0)
                windows.push({ slot: extra.id, window: extra.window })
        }
        for (var w = 0; w < windows.length; w++) {
            var entry = windows[w]
            var win = entry.window
            if (!win || typeof win.usedPercent !== "number"
                    || !isFinite(win.usedPercent) || win.usedPercent < 0) continue
            var reset = win.resetsAt ? new Date(win.resetsAt).getTime() : null
            if (reset !== null && (!isFinite(reset) || reset <= now)) continue
            // OpenRouter's normalized primary tracks keyUsageMonthly, but its
            // response has no reset timestamp. Its period is the UTC month.
            if (reset === null && record.id === "openrouter" && entry.slot === "primary") {
                var month = new Date(now)
                reset = month.getUTCFullYear() + "-" + month.getUTCMonth()
            }
            var key = JSON.stringify([record.id, record.sourceScope, accountId, entry.slot])
            var old = state[key]
            var level = win.usedPercent >= 95 ? 3 : win.usedPercent >= 80 ? 2
                : win.usedPercent >= PROJECTION_MIN_PERCENT
                    && Usage.projectedPercent(win, now) > 100 ? 1 : 0
            var samePeriod = old && old.reset === reset
            var priorLevel = samePeriod ? old.level : 0
            var pending = samePeriod ? old.pending : []
            var claimedLevel = Math.max.apply(Math, [priorLevel].concat(pending))
            // Unknown reset dates rearm only after observing fresh usage below 80%.
            var cycle = samePeriod ? old.cycle : {}
            if (reset === null && level === 0 && claimedLevel >= 2) {
                priorLevel = 0
                pending = []
                cycle = {}
                claimedLevel = 0
            }
            state[key] = { source: source, reset: reset, cycle: cycle,
                level: priorLevel, pending: pending }
            if (level <= claimedLevel) continue
            state[key].pending = pending.concat([level])
            var detail = level === 1 ? "Projected to exhaust before reset at the current pace."
                : Math.round(win.usedPercent) + "% of the allowance used."
            events.push({ kind: "usage", usageKey: key, cycle: cycle, level: level,
                title: "CodexBar " + (level === 3 ? "usage critical" : "usage warning"),
                // Never pass provider-supplied titles or unknown slot IDs into notification text.
                body: label + " · " + (Usage.windowLabel(record.id, entry.slot, win)
                    || "Usage window") + ": " + detail })
        }
    }
    return { state: state, events: events }
}

function delivery(previous, event, succeeded) {
    var old = event && previous[event.usageKey]
    // The in-memory cycle token also rejects callbacks after observed resets or scope retirement.
    if (!old || old.cycle !== event.cycle || old.pending.indexOf(event.level) < 0)
        return previous
    var state = Object.assign({}, previous)
    state[event.usageKey] = {
        source: old.source, reset: old.reset, cycle: old.cycle,
        level: succeeded ? Math.max(old.level, event.level) : old.level,
        pending: old.pending.filter(function(level) { return level !== event.level })
    }
    return state
}

function ompModeLabel(status) {
    if (!status || ["FILL", "TARGET", "EXPIRY-BURN", "BURN", "NO-BANK", "IDLE"]
            .indexOf(status.mode) < 0
            || ["auto", "confirm"].indexOf(status.operatingMode) < 0
            || typeof status.stalled !== "boolean") return null
    return status.operatingMode + " · "
        + (status.stalled ? "STALLED (" + status.mode + ")" : status.mode)
}

function ompMode(previous, status, enabled) {
    var old = previous || { mode: null, sequence: 0, transition: null, pending: false }
    var mode = enabled ? ompModeLabel(status) : null
    var state = { mode: mode, sequence: old.sequence, transition: null, pending: false }
    var events = []
    // Unavailable, disabled, and first observations establish a silent baseline.
    if (mode === null || old.mode === null) return { state: state, events: events }
    if (mode !== old.mode) {
        state.sequence++
        state.transition = { kind: "ompMode",
            sequence: state.sequence, title: "CodexBar omp mode changed",
            body: old.mode + " → " + mode }
    } else {
        state.transition = old.transition
        state.pending = old.pending
    }
    if (state.transition && !state.pending) {
        state.pending = true
        events.push(state.transition)
    }
    return { state: state, events: events }
}

function ompModeDelivery(previous, event, succeeded) {
    if (!previous || !previous.transition
            || previous.transition.sequence !== event.sequence) return previous
    return { mode: previous.mode, sequence: previous.sequence,
        transition: succeeded ? null : previous.transition, pending: false }
}

function agents(previous, records, enabled) {
    var old = previous || { enabled: false, sessions: {} }
    var sessions = {}
    var events = []
    for (var i = 0; i < records.length; i++) {
        var record = records[i]
        if (!record || typeof record.provider !== "string"
                || typeof record.sessionId !== "string" || !record.sessionId.trim()
                || record.sessionId.indexOf("untracked-") === 0
                || ["working", "blocked", "idle"].indexOf(record.state) < 0) continue
        var key = JSON.stringify([record.provider, record.sessionId])
        if (sessions[key] !== undefined) continue
        sessions[key] = record.state
        if (enabled && old.enabled && old.sessions[key] !== undefined
                && old.sessions[key] !== "blocked" && record.state === "blocked") {
            events.push({ kind: "agent",
                title: "CodexBar agent waiting for input",
                body: providerName(record.provider) + " is waiting for your input." })
        }
    }
    return { state: { enabled: enabled, sessions: sessions }, events: events }
}
