/* Shared by the customer dashboard and public store. Recheck settings on each action. */
(function () {
  "use strict";
  const acknowledged = new Set();

  function showMessage(message, canProceed) {
    return new Promise(resolve => {
      const dialog = document.createElement("dialog");
      dialog.style.cssText = "border:0;border-radius:18px;padding:24px;width:min(440px,90vw);box-shadow:0 20px 80px #0005;color:#0f172a";
      const title = document.createElement("h4");
      title.textContent = canProceed ? "First-Time Alert" : "Phone Verification";
      const text = document.createElement("p");
      text.textContent = message;
      const actions = document.createElement("div");
      actions.style.cssText = "display:flex;gap:12px;justify-content:flex-end";
      const cancel = document.createElement("button");
      cancel.className = "btn btn-outline-secondary";
      cancel.textContent = canProceed ? "Cancel" : "Close";
      actions.append(cancel);
      let answer = false;
      cancel.onclick = () => dialog.close();
      if (canProceed) {
        const proceed = document.createElement("button");
        proceed.className = "btn btn-primary";
        proceed.textContent = "Proceed";
        proceed.onclick = () => { answer = true; dialog.close(); };
        actions.append(proceed);
      }
      dialog.append(title, text, actions);
      dialog.addEventListener("close", () => { dialog.remove(); resolve(answer); }, { once: true });
      document.body.append(dialog);
      dialog.showModal();
      cancel.focus();
    });
  }

  function status(input, message, allowed) {
    if (!input) return;
    let node = input.parentElement.querySelector(".phone-history-status");
    if (!node) {
      node = document.createElement("div");
      node.setAttribute("aria-live", "polite");
      input.insertAdjacentElement("afterend", node);
    }
    node.className = "phone-history-status small mt-1 " + (allowed ? "text-success" : "text-danger");
    node.textContent = message || "";
  }

  window.verifyPhoneHistory = async function (line, input, preview) {
    if (["afa_registration", "results_checker", "bulk_sms_delivery"].includes(line.kind)
        || line.provider === "exosupplier" || (line.value_obj && line.value_obj.social_boosting)) return true;
    const serviceId = line.serviceId || line.service_id || line._id_str;
    // AFA, checker and SMS use separate forms rather than MTN service documents.
    if (!serviceId) return true;
    const phoneSnapshot = input ? input.value : null;
    const isCurrent = () => !input || input.value === phoneSnapshot;
    try {
      const response = await fetch("/api/phone/verify-existing-order", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ serviceId, phone: line.phone, source: "web_phone_check" })
      });
      const result = await response.json();
      if (!isCurrent()) return false;
      if (!response.ok || !result.success || !result.allow_order) {
        const message = result.message || "Unable to verify this number. Please try again.";
        status(input, message, false);
        if (!preview) await showMessage(message, false);
        return false;
      }
      status(input, result.message, !result.warning);
      if (result.warning && !preview) {
        const key = [result.phone, serviceId, result.warning_message].join("|");
        if (!acknowledged.has(key)) {
          if (!await showMessage(result.warning_message, true)) return false;
          acknowledged.add(key);
        }
        if (!isCurrent()) return false;
        line.phone_history_ack = result.acknowledgement_token;
      } else if (!result.warning) {
        delete line.phone_history_ack;
      }
      return true;
    } catch (error) {
      if (!isCurrent()) return false;
      const message = "Unable to verify number right now. Please try again.";
      status(input, message, false);
      if (!preview) await showMessage(message, false);
      return false;
    }
  };

  window.verifyPhoneHistoryCart = async function (items) {
    for (const line of items) {
      if (!await window.verifyPhoneHistory(line, null, false)) return false;
    }
    return true;
  };
})();
