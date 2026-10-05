// The create form's one enhancement (T3-W7): hide the gate fieldset while the
// selected type is not a gate. Without this script the fieldset stays visible,
// labelled "only for gates", and the server drops its fields for a non-gate.
(function () {
  "use strict";
  const form = document.querySelector("[data-create-form]");
  if (!form) return;
  const type = form.querySelector("[data-create-type]");
  const gate = form.querySelector("[data-create-gate]");
  if (!type || !gate) return;
  const sync = () => {
    gate.hidden = type.value !== "gate";
  };
  type.addEventListener("change", sync);
  sync();
})();
