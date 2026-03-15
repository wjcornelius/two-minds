// Offspring toolbar — injects persistent buttons into the chat header.
// Runs once after Chainlit's React app has rendered (via MutationObserver + timeout fallback).

(function () {
    function injectToolbar() {
        if (document.getElementById('offspring-toolbar')) return; // already injected

        var toolbar = document.createElement('div');
        toolbar.id = 'offspring-toolbar';

        // ── Open Biographer button ──────────────────────────────────────────────
        var bioBtn = document.createElement('button');
        bioBtn.id = 'offspring-bio-btn';
        bioBtn.textContent = '🧠 Open Biographer';
        bioBtn.title = 'Launch the Vector Biographer interview tool';

        var status = document.createElement('span');
        status.className = 'toolbar-status';
        status.id = 'offspring-toolbar-status';

        bioBtn.addEventListener('click', function () {
            bioBtn.disabled = true;
            bioBtn.textContent = 'Launching…';
            fetch('/launch-biographer', { method: 'POST' })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    bioBtn.textContent = '🧠 Open Biographer';
                    bioBtn.disabled = false;
                    if (data.status === 'already_running') {
                        status.textContent = '(already open)';
                        status.classList.add('visible');
                        setTimeout(function () { status.classList.remove('visible'); }, 4000);
                    } else {
                        // Window takes ~15 sec to fully load — keep message visible the whole time
                        status.textContent = '(opening — window appears in ~15 sec)';
                        status.classList.add('visible');
                        setTimeout(function () { status.classList.remove('visible'); }, 18000);
                    }
                })
                .catch(function () {
                    bioBtn.textContent = '🧠 Open Biographer';
                    bioBtn.disabled = false;
                    status.textContent = '(error — is server running?)';
                    status.classList.add('visible');
                    setTimeout(function () { status.classList.remove('visible'); }, 5000);
                });
        });

        // ── Stop All button ─────────────────────────────────────────────────────
        var stopBtn = document.createElement('button');
        stopBtn.id = 'offspring-stop-btn';
        stopBtn.textContent = '🛑Z Stop All';
        stopBtn.title = 'Shut down Chloe, Faith, and this chat server completely';

        stopBtn.addEventListener('click', function () {
            if (!confirm('Shut down Chloe, Faith, and the chat server?\n\nThis stops all background activity (fan noise, GPU use).\n\nTip: minimize this window instead to keep them running in the background.')) return;
            stopBtn.disabled = true;
            stopBtn.textContent = 'Stopping…';
            fetch('/stop-all', { method: 'POST' })
                .then(function (r) { return r.json(); })
                .then(function () {
                    stopBtn.textContent = '✓ Stopped';
                    status.textContent = '(closing window…)';
                    status.classList.add('visible');
                    setTimeout(function () { window.close(); }, 1500);
                })
                .catch(function () {
                    // Server killed itself before responding — that's expected and fine
                    stopBtn.textContent = '✓ Stopped';
                    status.textContent = '(closing window…)';
                    status.classList.add('visible');
                    setTimeout(function () { window.close(); }, 1500);
                });
        });

        // ── Hint text below Stop button ─────────────────────────────────────────
        var stopHint = document.createElement('span');
        stopHint.id = 'offspring-stop-hint';
        stopHint.textContent = '↓ minimize to keep running';

        toolbar.appendChild(bioBtn);
        toolbar.appendChild(stopBtn);
        toolbar.appendChild(stopHint);
        toolbar.appendChild(status);
        document.body.appendChild(toolbar);

        // ── Maximize window (reliable in Chrome --app mode) ──────────────────
        try {
            window.moveTo(0, 0);
            window.resizeTo(screen.width, screen.height);
        } catch (e) { /* ignore — not in popup context */ }

        // ── Welcome popup (shown on first visit; checkbox to suppress future shows) ──
        setTimeout(function () {
            if (localStorage.getItem('offspring_welcome_v2') === '1') return;

            var overlay = document.createElement('div');
            overlay.id = 'offspring-welcome-overlay';
            overlay.style.cssText = [
                'position:fixed;inset:0;background:rgba(0,0,0,0.65);z-index:999998',
                'display:flex;align-items:center;justify-content:center'
            ].join(';');

            var box = document.createElement('div');
            box.style.cssText = [
                'background:#0d1117;border:2px solid #2a3a5c;border-radius:12px',
                'padding:32px 36px;max-width:480px;width:90%;color:#dde6f0',
                'font-family:Segoe UI,system-ui,sans-serif;box-shadow:0 8px 40px rgba(0,0,0,0.7)'
            ].join(';');

            box.innerHTML = [
                '<h2 style="margin:0 0 16px;font-size:20px;color:#7eb8da">Welcome to Offspring</h2>',
                '<p style="margin:0 0 10px;font-size:14px;line-height:1.6">',
                'You\'re talking to <strong>Chloe</strong> and <strong>Faith</strong> — two AI companions',
                ' running on your computer. They learn over time and remember your conversations.',
                '</p>',
                '<p style="margin:0 0 6px;font-size:13px;color:#aac;font-weight:600">QUICK START</p>',
                '<ul style="margin:0 0 18px;padding-left:18px;font-size:13px;line-height:1.8;color:#ccd6e0">',
                '<li>Type to chat — select <strong>Chloe</strong>, <strong>Faith</strong>, or <strong>Family Chat</strong> above</li>',
                '<li>Click <strong>🧠 Open Biographer</strong> (top right) to record your life stories</li>',
                '<li>Click <strong>🛑Z Stop All</strong> to fully shut down and quiet the fan</li>',
                '<li>Close this window any time — they keep working in the background</li>',
                '</ul>',
                '<div style="display:flex;align-items:center;justify-content:space-between;margin-top:8px">',
                '<label style="font-size:12px;color:#8899bb;cursor:pointer;display:flex;align-items:center;gap:6px">',
                '<input type="checkbox" id="offspring-welcome-cb" style="cursor:pointer">',
                'Don\'t show this again',
                '</label>',
                '<button id="offspring-welcome-ok" style="background:#2a5c45;color:#e8f5e9;border:1px solid #3d8b65;',
                'border-radius:6px;padding:8px 24px;font-size:14px;font-weight:600;cursor:pointer">Got it</button>',
                '</div>'
            ].join('');

            overlay.appendChild(box);
            document.body.appendChild(overlay);

            document.getElementById('offspring-welcome-ok').addEventListener('click', function () {
                if (document.getElementById('offspring-welcome-cb').checked) {
                    localStorage.setItem('offspring_welcome_v2', '1');
                }
                overlay.remove();
            });

            // Also dismiss on backdrop click
            overlay.addEventListener('click', function (e) {
                if (e.target === overlay) overlay.remove();
            });
        }, 1200);

        // ── Keep window title descriptive so closing Chrome isn't ambiguous ─────
        function updateTitle() {
            if (document.title && !document.title.includes('·')) {
                document.title = 'Chloe & Faith Chat  ·  Daemons running  ·  Use 🛑Z to fully shut down';
            }
        }
        updateTitle();
        // Chainlit may reset the title on navigation — watch for it
        var titleObs = new MutationObserver(updateTitle);
        var titleEl = document.querySelector('title');
        if (titleEl) titleObs.observe(titleEl, { childList: true });
    }

    // Try immediately, then retry via MutationObserver in case React hasn't painted yet
    function tryInject() {
        injectToolbar();
        if (!document.getElementById('offspring-toolbar')) {
            var observer = new MutationObserver(function (mutations, obs) {
                injectToolbar();
                if (document.getElementById('offspring-toolbar')) {
                    obs.disconnect();
                }
            });
            observer.observe(document.body || document.documentElement, {
                childList: true,
                subtree: true
            });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', tryInject);
    } else {
        setTimeout(tryInject, 500);
    }
})();
