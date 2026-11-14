(function () {
  "use strict";

  document.addEventListener("change", function (event) {
    var target = event.target;
    if (target.matches && target.matches("select[data-autosubmit]") && target.form) {
      target.form.submit();
    }
  });

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (target.closest && target.closest("[data-print]")) {
      window.print();
    }
  });

  function formatSeconds(total) {
    var h = Math.floor(total / 3600);
    var m = Math.floor((total % 3600) / 60);
    var s = total % 60;
    return h + ":" + (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
  }

  var loadedAt = Date.now();
  var timers = document.querySelectorAll("[data-timer-seconds][data-timer-running='true']");
  if (timers.length) {
    setInterval(function () {
      var gone = Math.floor((Date.now() - loadedAt) / 1000);
      timers.forEach(function (el) {
        var start = parseInt(el.getAttribute("data-timer-seconds"), 10) || 0;
        el.textContent = formatSeconds(start + gone);
      });
    }, 1000);
  }

  document.addEventListener("input", function (event) {
    var slider = event.target;
    var outputId = slider.getAttribute && slider.getAttribute("data-slider-output");
    if (!outputId) return;
    var minutes = parseInt(slider.value, 10);
    var output = document.getElementById(outputId);
    if (output) {
      output.textContent = minutes >= parseInt(slider.max, 10) ? "3+ hours" : minutes + " min";
    }
    var use = slider.form && slider.form.querySelector("[data-slider-use]");
    if (use) use.checked = true;
  });

  var unsaved = false;
  document.addEventListener("input", function (event) {
    if (event.target.form && event.target.form.hasAttribute("data-unsaved-warning")) {
      unsaved = true;
    }
  });
  window.addEventListener("beforeunload", function (event) {
    if (unsaved) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  document.addEventListener("submit", function (event) {
    var form = event.target;
    var question = form.getAttribute && form.getAttribute("data-confirm");
    if (question && !window.confirm(question)) {
      event.preventDefault();
      return;
    }
    unsaved = false;
  });
})();
