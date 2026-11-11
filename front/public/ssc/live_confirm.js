// htmx's hx-confirm, in a dialog of our own.
(function () {
  const dialog = document.getElementById("live-confirm");
  if (!dialog) return;
  let pending = null;
  document.body.addEventListener("htmx:confirm", function (event) {
    if (!event.detail.question) return;
    event.preventDefault();
    pending = event.detail;
    document.getElementById("live-confirm-text").textContent = event.detail.question;
    dialog.showModal();
  });
  document.getElementById("live-confirm-no").addEventListener("click", () => dialog.close());
  // A tap on the scrim targets the dialog itself.
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) dialog.close();
  });
  document.getElementById("live-confirm-yes").addEventListener("click", function () {
    dialog.close();
    if (pending) pending.issueRequest(true);
    pending = null;
  });
})();
