(function (window, document) {
  "use strict";

  var modal = document.getElementById("keypad-modal");
  if (!modal) {
    return;
  }

  var titleEl = document.getElementById("keypad-item");
  var displayEl = document.getElementById("keypad-display");
  var okButton = document.getElementById("keypad-ok");
  var cancelButton = document.getElementById("keypad-cancel");

  var typed = "";
  var startingValue = 0;
  var onOk = null;

  function render() {
    displayEl.textContent = typed === "" ? String(startingValue) : typed;
  }

  function open(options) {
    titleEl.textContent = options.title || "";
    startingValue = options.value || 0;
    onOk = options.onOk || null;

    typed = "";
    render();

    modal.hidden = false;
    document.body.classList.add("modal-open");
  }

  function close() {
    modal.hidden = true;
    document.body.classList.remove("modal-open");
    onOk = null;
  }

  function press(key) {
    if (key === "clear") {
      typed = "0";
    } else if (key === "back") {
      typed = (typed === "" ? String(startingValue) : typed).slice(0, -1);
    } else {

      if (typed.length < 6) {
        typed = (typed === "0" ? "" : typed) + key;
      }
    }
    render();
  }

  function confirm() {
    var value = typed === "" ? startingValue : parseInt(typed, 10);
    if (isNaN(value) || value < 0) {
      value = 0;
    }
    var callback = onOk;
    close();
    if (callback) {
      callback(value);
    }
  }

  modal.addEventListener("click", function (event) {
    var key = event.target.closest("[data-key]");
    if (key) {
      press(key.getAttribute("data-key"));
      return;
    }

    if (event.target === modal) {
      close();
    }
  });

  okButton.addEventListener("click", confirm);
  cancelButton.addEventListener("click", close);

  document.addEventListener("keydown", function (event) {
    if (modal.hidden) {
      return;
    }
    if (event.key >= "0" && event.key <= "9") {
      press(event.key);
    } else if (event.key === "Backspace") {
      press("back");
      event.preventDefault();
    } else if (event.key === "Enter") {
      confirm();
    } else if (event.key === "Escape") {
      close();
    }
  });

  window.Keypad = { open: open, close: close };
})(window, document);
