.pragma library

function windowLabel(provider, slot, window, extraTitle) {
    if (provider === "codex" && (slot === "codex-base-model-inference"
            || slot === "gpt-reserve" || extraTitle === "gpt-reserve")) return "Reserve 7d"
    if (extraTitle && extraTitle.length > 0) return extraTitle
    if (slot === "claude-design") return "Design"
    if (slot === "claude-routines") return "Routines"
    if (provider === "claude" && slot === "tertiary") return "Sonnet"
    if (provider === "openrouter") return "Limit"
    if (provider === "kilo") return "Credits"
    if (provider === "zai" && slot === "secondary") return "Monthly"
    var minutes = window && window.windowMinutes
    if (minutes === 300) return "5h"
    if (minutes === 1440) return "1d"
    if (minutes === 10080) return "7d"
    if (minutes === 43200) return "Monthly"
    return ""
}

// Shared by the displayed pace and usage notifications.
function windowSpanMs(win) {
    if (win.startsAt && win.resetsAt)
        return new Date(win.resetsAt).getTime() - new Date(win.startsAt).getTime()
    return (win.windowMinutes || 0) * 60000
}

function pacePercent(win, now) {
    if (!win || !win.resetsAt || !win.windowMinutes) return -1
    var windowMs = windowSpanMs(win)
    var remainingMs = new Date(win.resetsAt).getTime() - now
    if (isNaN(remainingMs) || remainingMs <= 0 || remainingMs > windowMs) return -1
    var pacePct = (1 - remainingMs / windowMs) * 100
    return pacePct < 3 ? -1 : pacePct
}

function projectedPercent(win, now) {
    var pacePct = pacePercent(win, now)
    if (pacePct < 0) return -1
    return Math.min(999, (win.usedPercent || 0) * 100 / pacePct)
}
