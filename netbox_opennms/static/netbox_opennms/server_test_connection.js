// Copyright 2026 Ronny Trommer <ronny@no42.org>
// SPDX-License-Identifier: MIT
//
// Add/edit form for an OpenNMS Server: a "Test connection" button that posts
// the in-progress url/username/password/headers to opennmsserver_test_ajax
// and, on success, populates the "Default location" <select> with the live
// location list. The select is never HTML-`disabled` (that would drop it
// from form submission) — this script only toggles the CSS class the form
// renders it with by default ("onms-location-pending") so an existing
// Server's saved value keeps posting normally until a test is (re-)run.
(function () {
  function getCookie(name) {
    const match = document.cookie.match(
      new RegExp("(^| )" + name + "=([^;]+)")
    );
    return match ? decodeURIComponent(match[2]) : null;
  }

  function fieldValue(id) {
    const el = document.getElementById(id);
    return el ? el.value : "";
  }

  document.addEventListener("DOMContentLoaded", function () {
    const passwordField = document.getElementById("id_password");
    const locationSelect = document.getElementById("id_default_location");
    if (!passwordField || !locationSelect) {
      return;
    }
    const fieldGroup =
      passwordField.closest(".mb-3, .form-group") || passwordField.parentElement;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn btn-outline-primary mt-2";
    button.textContent = "Test connection";

    const status = document.createElement("span");
    status.className = "ms-2";

    fieldGroup.appendChild(button);
    fieldGroup.appendChild(status);

    button.addEventListener("click", function () {
      button.disabled = true;
      status.textContent = "Testing…";
      status.className = "ms-2 text-muted";

      const body = new URLSearchParams({
        url: fieldValue("id_url"),
        username: fieldValue("id_username"),
        password: fieldValue("id_password"),
        headers: fieldValue("id_headers"),
        server_id: window.OPENNMS_SERVER_ID || "",
      });

      fetch(window.OPENNMS_SERVER_TEST_AJAX_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": getCookie("csrftoken"),
        },
        body: body.toString(),
      })
        .then(function (response) {
          return response.json();
        })
        .then(function (data) {
          button.disabled = false;
          if (data.ok) {
            const current = locationSelect.value;
            locationSelect.innerHTML = "";
            (data.locations || []).forEach(function (loc) {
              const option = document.createElement("option");
              option.value = loc;
              option.textContent = loc;
              locationSelect.appendChild(option);
            });
            if (
              current &&
              (data.locations || []).indexOf(current) !== -1
            ) {
              locationSelect.value = current;
            }
            locationSelect.classList.remove("onms-location-pending");
            status.textContent = "Connection OK.";
            status.className = "ms-2 text-success";
          } else {
            status.textContent = data.message || "Connection failed.";
            status.className = "ms-2 text-danger";
          }
        })
        .catch(function () {
          button.disabled = false;
          status.textContent = "Connection test failed to run.";
          status.className = "ms-2 text-danger";
        });
    });
  });
})();
