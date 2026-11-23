(() => {
  if (globalThis.__WEB_AGENT_SWARM_CONTENT__) return;
  globalThis.__WEB_AGENT_SWARM_CONTENT__ = true;

  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

  function composer() {
    return (
      document.querySelector('#prompt-textarea') ||
      document.querySelector('div[contenteditable="true"][role="textbox"]') ||
      document.querySelector('textarea')
    );
  }

  function sendButton() {
    return (
      document.querySelector('[data-testid="send-button"]') ||
      document.querySelector('button[aria-label*="Send"]') ||
      document.querySelector('button[aria-label*="发送"]')
    );
  }

  function generating() {
    return Boolean(
      document.querySelector('[data-testid="stop-button"]') ||
      document.querySelector('button[aria-label*="Stop generating"]') ||
      document.querySelector('button[aria-label*="停止生成"]')
    );
  }

  function boundedText(value, limit = 12000) {
    const text = String(value || '').replace(/\u00a0/g, ' ').trim();
    return text.length <= limit ? text : text.slice(0, limit) + '\n...[truncated]';
  }

  function snapshot(tail = 6) {
    const n = Math.max(1, Math.min(Number(tail) || 6, 20));
    const messages = Array.from(
      document.querySelectorAll('[data-message-author-role]')
    )
      .filter(node => ['user', 'assistant'].includes(
        node.getAttribute('data-message-author-role')
      ))
      .map((node, index) => ({
        index,
        role: node.getAttribute('data-message-author-role') || '',
        messageId:
          node.getAttribute('data-message-id') ||
          node.closest('[data-message-id]')?.getAttribute('data-message-id') ||
          '',
        text: boundedText(node.innerText || node.textContent || '')
      }));

    const last = messages[messages.length - 1] || null;
    const lastUser = [...messages].reverse().find(item => item.role === 'user') || null;
    const lastAssistant = [...messages].reverse().find(item => item.role === 'assistant') || null;
    const busy = generating();
    const visibleText = String(document.body?.innerText || '');
    const connectionInterrupted = (
      visibleText.includes('连接已中断') ||
      visibleText.includes('正在等待完整回复') ||
      visibleText.includes('Connection interrupted') ||
      visibleText.includes('waiting for a complete response')
    );
    const retryActionPresent = Array.from(document.querySelectorAll('button')).some(button => {
      const label = String(
        button.innerText || button.textContent || button.getAttribute('aria-label') || ''
      ).trim().toLowerCase();
      return label === '重试' || label === 'retry' || label.includes('retry');
    });
    const requestFailed = Boolean(
      !busy && retryActionPresent && (
        visibleText.includes('消息发送超时') ||
        visibleText.includes('请重试') ||
        visibleText.includes('Message send timed out') ||
        visibleText.includes('Please retry')
      )
    );
    const conversationLimitReached = (
      visibleText.includes('你已达到此对话的长度上限') ||
      visibleText.includes('达到此对话的长度上限') ||
      visibleText.includes('开始新聊天以继续对话') ||
      visibleText.includes("You've reached the maximum length for this conversation") ||
      visibleText.includes('You have reached the maximum length for this conversation') ||
      visibleText.includes('start a new chat to continue')
    );

    return {
      title: document.title,
      url: location.href,
      uiState: conversationLimitReached
        ? 'conversation_limit'
        : (busy ? 'generating' : 'idle'),
      generating: busy,
      connectionInterrupted,
      requestFailed,
      retryActionPresent,
      conversationLimitReached,
      stuckGenerating: Boolean(busy && connectionInterrupted),
      composerPresent: Boolean(composer()),
      sendButtonEnabled: Boolean(sendButton() && !sendButton().disabled),
      messageCount: messages.length,
      lastRole: last?.role || null,
      lastUserText: lastUser?.text || '',
      lastAssistantText: lastAssistant?.text || '',
      messages: messages.slice(-n)
    };
  }

  async function setText(target, text) {
    target.focus();
    if (target instanceof HTMLTextAreaElement || target instanceof HTMLInputElement) {
      const proto = target instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
      descriptor?.set?.call(target, text);
      target.dispatchEvent(new Event('input', { bubbles: true }));
      target.dispatchEvent(new Event('change', { bubbles: true }));
      return;
    }

    target.replaceChildren();
    const p = document.createElement('p');
    p.textContent = text;
    target.appendChild(p);
    target.dispatchEvent(new InputEvent('input', {
      bubbles: true,
      inputType: 'insertText',
      data: text
    }));
  }

  const ACTIVE_SEND_KEY = '__was_cr01_active_send_v1';
  const CONSUMED_SEND_PREFIX = '__was_cr01_attempted_send_v1:';

  function validConversationUrl(raw) {
    try {
      const url = new URL(raw);
      return url.origin === 'https://chatgpt.com'
        && !url.search && !url.hash
        && (url.pathname === '/'
          || /^\/c\/[^/]+\/?$/.test(url.pathname)
          || /^\/g\/[^/]+(?:\/c\/[^/]+)?\/?$/.test(url.pathname));
    } catch {
      return false;
    }
  }

  function composerText(target) {
    if (typeof target?.value === 'string') return target.value;
    return String(target?.innerText ?? target?.textContent ?? '');
  }

  function occupiedComposer(target) {
    // A text-empty composer containing attachments is NOT an empty draft.
    // Preserve even whitespace-only/zero-width owner input byte-for-byte.
    // Failing closed on an unfamiliar placeholder is safer than overwriting it.
    if (composerText(target).length > 0) return true;
    try {
      if (target.querySelector?.('img,video,canvas,button,[data-file-id],[data-testid*="attachment"]')) {
        return true;
      }
      if (document.querySelector(
        '[data-testid="composer-attachments"],' +
        '[data-testid="attachment-chip"],' +
        '[data-testid="file-upload-preview"]'
      )) return true;
    } catch {
      // Unknown editor structure must not justify overwriting the owner.
      return true;
    }
    return false;
  }

  async function sendMessage(text, expectedUrl, commandId) {
    const cleanText = String(text || '');
    const cleanCommand = String(commandId || '');
    const fail = error => ({ ok: false, error, mayHaveExecuted: false });
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(cleanCommand)) {
      return fail('A valid unique command ID is required before Send.');
    }
    if (!cleanText.trim() || cleanText.length > 12000) {
      return fail('Send text is empty or exceeds the configured limit.');
    }
    // Require a pinned stable conversation, not merely a ChatGPT-origin tab.
    if (!validConversationUrl(expectedUrl) || location.href !== expectedUrl) {
      return fail('Conversation URL does not match the pinned recipient.');
    }

    const consumedKey = CONSUMED_SEND_PREFIX + cleanCommand;
    // sessionStorage is synchronous, tab-scoped and normally shared between
    // extension content-script worlds. If unavailable, do not risk a click.
    try {
      if (sessionStorage.getItem(consumedKey)) {
        return { ok: false, error: 'Send command already attempted.',
          mayHaveExecuted: true, terminal: true };
      }
      if (sessionStorage.getItem(ACTIVE_SEND_KEY)) {
        return fail('Another Send is active in this tab.');
      }
      sessionStorage.setItem(ACTIVE_SEND_KEY, cleanCommand);
      if (sessionStorage.getItem(ACTIVE_SEND_KEY) !== cleanCommand) {
        return fail('Could not reserve the tab Send boundary.');
      }
    } catch {
      return fail('Tab-scoped Send storage unavailable; no click attempted.');
    }

    try {
      if (generating()) return fail('Target tab is generating.');
      let target = composer();
      for (let i = 0; i < 20 && !target; i += 1) {
        await sleep(250);
        if (location.href !== expectedUrl) return fail('Target navigated while waiting.');
        target = composer();
      }
      if (!target) return fail('Composer not found.');
      if (target.isConnected === false || occupiedComposer(target)) {
        return fail('Owner draft or attachment present; Send refused.');
      }
      // Check the tab and owner draft again immediately before changing DOM.
      if (location.href !== expectedUrl || composer() !== target || generating()) {
        return fail('Recipient or editor changed before composition.');
      }

      await setText(target, cleanText);
      await sleep(250);

      let button = sendButton();
      for (let i = 0; i < 12 && (!button || button.disabled); i += 1) {
        await sleep(150);
        if (location.href !== expectedUrl) return fail('Recipient navigated before Send.');
        button = sendButton();
      }
      if (!button || button.disabled) return fail('Send button unavailable or disabled.');
      if (target.isConnected === false || composer() !== target ||
          location.href !== expectedUrl || generating()) {
        return fail('Recipient or editor changed before final Send.');
      }
      if (composerText(target) !== cleanText) {
        return fail('Composer modified by another actor; Send refused.');
      }
      if (document.querySelector(
        '[data-testid="composer-attachments"],' +
        '[data-testid="attachment-chip"],' +
        '[data-testid="file-upload-preview"]'
      )) return fail('Attachment appeared during composition; Send refused.');

      // This storage write is the physical Send boundary. Commit before click,
      // so a lost acknowledgement, service-worker restart or second extension
      // cannot replay the same command. It intentionally sacrifices liveness
      // if a crash happens after this marker but before the click.
      try {
        sessionStorage.setItem(consumedKey, expectedUrl);
        if (sessionStorage.getItem(consumedKey) !== expectedUrl) {
          return fail('Could not persist physical Send marker.');
        }
      } catch {
        return fail('Physical Send marker unavailable; no click attempted.');
      }

      try {
        button.click();
        return { ok: true, mayHaveExecuted: true, terminal: true };
      } catch (error) {
        return { ok: false, error: 'Click outcome unknown: ' + String(error),
          mayHaveExecuted: true, terminal: true };
      }
    } finally {
      try {
        if (sessionStorage.getItem(ACTIVE_SEND_KEY) === cleanCommand) {
          sessionStorage.removeItem(ACTIVE_SEND_KEY);
        }
      } catch {
        // A lost tab-scoped marker is not permission to retry.
      }
    }
  }

  chrome.runtime.onMessage.addListener((message, _sender, respond) => {
    if (message?.type === 'inspect-chatgpt') {
      try {
        respond({ ok: true, snapshot: snapshot(message.tail) });
      } catch (error) {
        respond({ ok: false, error: String(error) });
      }
      return;
    }

    if (message?.type === 'send-chatgpt-message') {
      sendMessage(String(message.text || ''), message.expectedUrl, message.commandId)
        .then(respond)
        .catch(error => respond({ ok: false, error: String(error) }));
      return true;
    }
  });
})();
