/* The Overview's activity chart.
 *
 * An enhancement, never the content. The metrics beside it are server-rendered
 * HTML; if this file fails to load, is blocked by a corporate proxy, or throws,
 * the page still says everything §17 asks for. Nothing here writes text a user
 * needs to read.
 *
 * Data arrives in a JSON script tag rather than in an inline script. The server
 * escapes it as JSON, so a subject line or an address can never break out of a
 * string literal and become code - which is the whole reason not to interpolate
 * server data into JavaScript.
 */
(function () {
  "use strict";

  function readSeries() {
    var node = document.getElementById("activity-data");
    if (!node) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (error) {
      // A malformed island is a bug worth seeing in the console, but it must
      // not take the rest of the page's scripts down with it.
      console.error("[seskit] activity data could not be parsed", error);
      return null;
    }
  }

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement)
      .getPropertyValue(name)
      .trim();
    return value || fallback;
  }

  function render() {
    var canvas = document.getElementById("activity-chart");
    if (!canvas || typeof Chart === "undefined") return;

    var series = readSeries();
    if (!series || !series.points || series.points.length === 0) return;

    var labels = series.points.map(function (point) {
      return point.label;
    });

    // Colours come from the same tokens the rest of the dashboard uses, so the
    // chart themes with everything else instead of carrying its own palette.
    var dataset = function (label, key, colour) {
      return {
        label: label,
        data: series.points.map(function (point) {
          return point[key];
        }),
        borderColor: colour,
        backgroundColor: colour,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 3,
        tension: 0.25,
      };
    };

    new Chart(canvas, {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          dataset("Sent", "sent", cssVar("--accent", "#3355ff")),
          dataset("Delivered", "delivered", cssVar("--success", "#067647")),
          dataset("Bounced", "bounced", cssVar("--danger", "#b42318")),
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            position: "bottom",
            labels: { boxWidth: 8, boxHeight: 8, usePointStyle: true },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { maxRotation: 0, autoSkipPadding: 24 },
          },
          y: {
            beginAtZero: true,
            // Counts are whole numbers; a y-axis offering 0.5 of an email is
            // the sort of detail that makes a dashboard look unconsidered.
            ticks: { precision: 0 },
            border: { display: false },
          },
        },
      },
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
