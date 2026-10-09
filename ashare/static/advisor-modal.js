(() => {
  const dialog = document.getElementById('advisor-dialog');
  const frame = document.getElementById('advisor-frame');
  let opener = null;
  const focusQuestion = () => {
    if (dialog.open) frame.contentDocument?.getElementById('question')?.focus();
  };
  document.querySelectorAll('[data-open-advisor]').forEach(button => {
    button.addEventListener('click', () => {
      if (dialog.open) return;
      opener = button;
      dialog.showModal();
      document.documentElement.classList.add('advisor-open');
      if (!frame.hasAttribute('src')) frame.src = frame.dataset.src;
      else focusQuestion();
    });
  });
  frame.addEventListener('load', focusQuestion);
  document.getElementById('close-advisor').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right ||
        event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  });
  dialog.addEventListener('close', () => {
    document.documentElement.classList.remove('advisor-open');
    opener?.focus();
  });
  window.addEventListener('message', event => {
    if (event.origin === location.origin && event.source === frame.contentWindow &&
        event.data?.type === 'openpike-advisor-close' && dialog.open) dialog.close();
  });
})();
