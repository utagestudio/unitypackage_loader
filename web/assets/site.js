// Theme toggle, copy-to-clipboard and the before/after slider. The initial theme is applied by the inline
// script in <head> (so the page does not flash); this file only handles interaction.
(function () {
  var root = document.documentElement;

  // Respect "reduce motion": keep the demo on its poster and let the viewer start it.
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    Array.prototype.forEach.call(document.querySelectorAll("video.demo-video"), function (v) {
      v.removeAttribute("autoplay");
      v.pause();
      v.controls = true;
    });
  }

  var sliders = document.querySelectorAll(".ba");
  Array.prototype.forEach.call(sliders, function (box) {
    var range = box.querySelector(".ba-range");
    if (!range) return;
    function update() { box.style.setProperty("--pos", range.value + "%"); }
    range.addEventListener("input", update);
    update();
  });

  function currentTheme() {
    var stamped = root.getAttribute("data-theme");
    if (stamped === "dark" || stamped === "light") return stamped;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("theme", next); } catch (e) {}
    });
  }

  var copies = document.querySelectorAll("[data-copy]");
  Array.prototype.forEach.call(copies, function (btn) {
    var label = btn.textContent;
    btn.addEventListener("click", function () {
      var text = btn.getAttribute("data-copy");
      function done() {
        btn.setAttribute("data-copied", "true");
        btn.textContent = btn.getAttribute("data-copied-label") || "Copied";
        setTimeout(function () { btn.removeAttribute("data-copied"); btn.textContent = label; }, 1800);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { window.prompt("Copy this URL:", text); });
      } else {
        window.prompt("Copy this URL:", text);
      }
    });
  });
})();
