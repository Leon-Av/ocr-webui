// Общие JS-утилиты для OCR-сервиса.

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' Б';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' КБ';
    if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + ' МБ';
    return (bytes / 1024 / 1024 / 1024).toFixed(2) + ' ГБ';
}

// Звуковой сигнал завершения задачи (короткий base64-WAV).
const NOTIF_SOUND =
    'data:audio/wav;base64,UklGRiQEAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAEAAA' +
    'AAQAhALgBtwG4AbwBugG+AcABwgHEAcYByAHIAcwBzQHNAcwByAHIAcgByAHIAcgByAHIAcgByAHIAcgAB' +
    'AAEA/f/x//r/+f/4//f/9v/1//T/8//y//H/8P/v/+7/7f/q/+n/6P/n/+b/5f/j/+L/4f/g/+D/3//e' +
    '/93/3v/e/9//3//f/9//4P/h/+P/5P/l/+X/5v/o/+n/6v/r/+z/7f/u/+//8P/w//H/8v/z//T/9f/2' +
    '//f/+P/5//r/+/z8//7//wAAAAE=';

let _audioCtx = null;
function playNotification() {
    try {
        if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const ctx = _audioCtx;
        const o = ctx.createOscillator();
        const g = ctx.createGain();
        o.connect(g); g.connect(ctx.destination);
        o.type = 'sine'; o.frequency.value = 880;
        g.gain.setValueAtTime(0.001, ctx.currentTime);
        g.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + 0.01);
        g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
        o.start(); o.stop(ctx.currentTime + 0.4);
    } catch (e) { /* звук недоступен */ }
}

function showBrowserNotification(title, body) {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'granted') {
        try { new Notification(title, { body, icon: '/static/favicon.svg' }); } catch (e) {}
    }
}
