export async function requestAIStream(url, options, onEvent) {
  const controller = new AbortController();
  const externalSignal = options.signal;
  const abort = () => controller.abort();
  let timedOut = false;
  let timer;
  let reader;
  let timeoutSeconds = 60;
  let generationStarted = false;
  const armTimeout = milliseconds => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, milliseconds);
  };
  // Covers a request that never receives metadata, including a broken proxy.
  armTimeout(615000);
  if (externalSignal) {
    externalSignal.addEventListener('abort', abort);
    if (externalSignal.aborted) controller.abort();
  }
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    if (!response.ok) {
      const problem = await response.json();
      throw new Error(problem.description || problem.title || 'Unable to send message');
    }
    if (!response.body) throw new Error('Unable to send message');
    reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) throw new Error('AI stream ended before completion');
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const raw = buffer.slice(0, boundary.index);
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const eventName = (raw.match(/^event:\s*(.+)$/m) || [])[1];
        const dataLines = raw.split(/\r?\n/).filter(line => line.startsWith('data:'));
        if (!dataLines.length) continue;
        const data = JSON.parse(dataLines.map(line => line.slice(5).trimStart()).join('\n'));
        if (eventName === 'meta') {
          if (Number.isFinite(data.timeout_seconds) && data.timeout_seconds > 0) {
            timeoutSeconds = Math.min(data.timeout_seconds, 600);
          }
          armTimeout((timeoutSeconds + 15) * 1000);
        }
        if (eventName === 'status' && data.stage === 'waiting_for_model' && !generationStarted) {
          generationStarted = true;
          armTimeout((timeoutSeconds + 5) * 1000);
        }
        if (eventName === 'error') throw new Error(data.message || 'Unable to send message');
        onEvent(eventName, data);
        // A completed SSE response need not close its HTTP connection.
        if (eventName === 'done') return;
      }
    }
  } catch (error) {
    if (timedOut) throw new Error('AI response timed out. Please retry or check the model timeout setting.');
    throw error;
  } finally {
    clearTimeout(timer);
    if (externalSignal) externalSignal.removeEventListener('abort', abort);
    controller.abort();
    if (reader) reader.releaseLock();
  }
}
