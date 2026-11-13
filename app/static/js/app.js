/*
 * Small behaviours used on many pages.
 *
 * These used to be inline attributes (onchange="...", onsubmit="..."). The
 * Content-Security-Policy now allows only scripts served from this site --
 * no inline JavaScript at all -- so a template marks what it wants with a
 * data- attribute and this file does it:
 *
 *   <select data-autosubmit>       submits its form as soon as it changes
 *   <form data-confirm="Sure?">    asks first, and only submits on OK
 *   <button data-print>            opens the browser's print dialog
 *   <span data-timer-seconds="75"  counts up once a second while
 *         data-timer-running="true">  running. Display only: the time
 *                                  itself is worked out on the server.
 *   <input type=range data-slider-output="id">  shows its value as minutes
 *                                  and ticks the [data-slider-use] box
 *   <form data-unsaved-warning>    if something was typed and the page is
 *                                  left by a link (not a button on the
 *                                  page), the browser asks first
 */
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

  // Count from when the page loaded, not from the browser's clock, so a
  // phone with the wrong time still shows the server's number.
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

  // Typed-but-unsaved answers. Every button on the intake page submits a
  // form, and the server keeps the answers, so only leaving by a link (the
  // top bar, the back button) could lose them.
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
    unsaved = false;  // the page is being left by one of its own buttons
  });
})();
