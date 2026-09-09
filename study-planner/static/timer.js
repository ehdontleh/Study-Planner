document.addEventListener('DOMContentLoaded', () => {
  const timers = {}; // task_id -> { sessionId, startMs, interval }

  function formatElapsed(ms) {
    const totalSeconds = Math.floor(ms / 1000);
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = totalSeconds % 60;
    const pad = n => String(n).padStart(2, '0');
    return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  document.querySelectorAll('[data-start-task]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const taskId = btn.dataset.startTask;
      const startedAt = new Date().toISOString();
      try {
        const res = await fetch('/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ task_id: taskId, started_at: startedAt })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'start failed');

        timers[taskId] = { sessionId: data.session_id, startMs: Date.now() };
        btn.disabled = true;

        const stopBtn = document.querySelector(`[data-stop-task="${taskId}"]`);
        if (stopBtn) stopBtn.disabled = false;

        const display = document.querySelector(`[data-timer-display="${taskId}"]`);
        if (display) display.textContent = '00:00';

        timers[taskId].interval = setInterval(() => {
          if (display) display.textContent = formatElapsed(Date.now() - timers[taskId].startMs);
        }, 1000);
      } catch (e) {
        alert('Could not start the timer — please refresh and try again.');
      }
    });
  });

  document.querySelectorAll('[data-stop-task]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const taskId = btn.dataset.stopTask;
      const timer = timers[taskId];
      if (!timer) return;

      clearInterval(timer.interval);
      const elapsedMs = Date.now() - timer.startMs;
      const stoppedAt = new Date().toISOString();

      try {
        await fetch('/stop', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: timer.sessionId, stopped_at: stoppedAt })
        });
      } catch (e) {
        // Session end failing to log server-side shouldn't block the local flow.
      }

      btn.disabled = true;
      const display = document.querySelector(`[data-timer-display="${taskId}"]`);
      if (display) display.textContent = formatElapsed(elapsedMs);

      const minutesInput = document.querySelector(`[data-minutes-input="${taskId}"]`);
      if (minutesInput) minutesInput.value = Math.max(1, Math.round(elapsedMs / 60000));

      delete timers[taskId];
    });
  });

  // On submit, turn the (auto-filled or manually typed) minutes value into
  // the milliseconds figure the backend stores.
  document.querySelectorAll('[data-complete-form]').forEach(form => {
    form.addEventListener('submit', () => {
      const taskId = form.dataset.completeForm;
      const minutesInput = document.querySelector(`[data-minutes-input="${taskId}"]`);
      const hiddenInput = document.querySelector(`[data-time-studied-input="${taskId}"]`);
      const minutes = minutesInput ? parseFloat(minutesInput.value) || 0 : 0;
      if (hiddenInput) hiddenInput.value = Math.round(minutes * 60000);
    });
  });
});
