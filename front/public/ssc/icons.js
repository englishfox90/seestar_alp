// Puts the icon sprite in the page, so <use href="#name"> works everywhere, including in
// polled fragments. It runs as the body opens: from the second page of a session the
// icons are there at first paint, because a tab bar that blinks on each tap is noise.
(function () {
  const KEY = "ssc-icons";
  const rootPath = document.documentElement.dataset.rootPath || "";

  function insert(svg) {
    document.body.insertAdjacentHTML("afterbegin", svg);
  }

  let held = null;
  try {
    held = sessionStorage.getItem(KEY);
  } catch (err) {
    // Storage can be refused. The sprite then comes from the HTTP cache each time.
  }
  if (held) {
    insert(held);
    return;
  }
  fetch(rootPath + "/public/ssc/icons.svg")
    .then(function (reply) {
      if (!reply.ok) throw new Error("icons: " + reply.status);
      return reply.text();
    })
    .then(function (svg) {
      insert(svg);
      try {
        sessionStorage.setItem(KEY, svg);
      } catch (err) {
        // As above.
      }
    })
    .catch(function (err) {
      // Every icon sits beside its word, so the page still says everything.
      console.error("Could not load icons", err);
    });
})();
