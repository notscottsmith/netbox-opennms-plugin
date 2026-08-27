// Copyright 2026 Ronny Trommer <ronny@no42.org>
// SPDX-License-Identifier: MIT
//
// Add/edit form for a Discovery Scan: populates the "Location" <select> from
// the chosen Server's own live OpenNMS Monitoring Locations
// (discoveryscan_server_locations_ajax), mirroring how
// server_test_connection.js populates OpenNMSServerForm.default_location.
// The select is never HTML-`disabled` (that would drop it from form
// submission) — this script only toggles the "onms-location-pending" CSS
// class the form renders it with by default, so an existing Discovery Scan's
// saved value keeps posting normally until a Server is (re-)picked.
(function () {
  document.addEventListener("DOMContentLoaded", function () {
    const serverSelect = document.getElementById("id_server");
    const locationSelect = document.getElementById("id_location");
    if (!serverSelect || !locationSelect) {
      return;
    }

    function populate(serverId) {
      if (!serverId) {
        return;
      }
      const url = new URL(window.DISCOVERYSCAN_SERVER_LOCATIONS_AJAX_URL, window.location.origin);
      url.searchParams.set("server_id", serverId);

      fetch(url.toString(), { headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (response) {
          return response.json();
        })
        .then(function (data) {
          if (!data.ok) {
            return;
          }
          const current = locationSelect.value;
          locationSelect.innerHTML = "";
          (data.locations || []).forEach(function (loc) {
            const option = document.createElement("option");
            option.value = loc;
            option.textContent = loc;
            locationSelect.appendChild(option);
          });
          if (current && (data.locations || []).indexOf(current) !== -1) {
            locationSelect.value = current;
          }
          locationSelect.classList.remove("onms-location-pending");
        })
        .catch(function () {
          // Leave the existing/pending choices in place on failure.
        });
    }

    serverSelect.addEventListener("change", function () {
      populate(serverSelect.value);
    });

    // NetBox's dynamic-select widget (select2) wraps the native <select> and
    // fires jQuery events on it rather than a plain DOM "change" in every
    // case — listen there too when jQuery is present.
    if (window.jQuery) {
      window.jQuery(serverSelect).on("change", function () {
        populate(serverSelect.value);
      });
    }

    if (serverSelect.value) {
      populate(serverSelect.value);
    }
  });
})();
