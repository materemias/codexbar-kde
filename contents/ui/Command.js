.pragma library

function shellQuote(value) {
    return "'" + String(value).replace(/'/g, "'\\''") + "'"
}

function localPath(url) {
    return decodeURIComponent(url.toString().replace(/^file:\/\//, ""))
}

function cliPath(configured) {
    return configured || "/usr/bin/codexbar"
}
