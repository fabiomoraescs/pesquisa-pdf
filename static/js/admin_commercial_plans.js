(() => {
  "use strict";

  const dialogs = [...document.querySelectorAll("[data-commercial-limit-modal]")];
  if (!dialogs.length) return;

  const close = (dialog) => {
    if (dialog && dialog.open) dialog.close();
  };

  document.querySelectorAll("[data-commercial-limit-open]").forEach((button) => {
    button.addEventListener("click", () => {
      const dialog = document.getElementById(button.dataset.commercialLimitOpen);
      if (dialog && typeof dialog.showModal === "function") dialog.showModal();
    });
  });

  dialogs.forEach((dialog) => {
    dialog.querySelectorAll("[data-commercial-limit-close]").forEach((button) => {
      button.addEventListener("click", () => close(dialog));
    });
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) close(dialog);
    });
  });
})();
