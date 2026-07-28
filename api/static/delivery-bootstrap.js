(() => {
  "use strict";

  const unavailableMessage =
    "This delivery link is unavailable. It may have expired or been withdrawn. "
    + "Ask the person who sent it to create a new link.";
  const statusElement = document.querySelector("#delivery-status");

  function showUnavailable() {
    if (statusElement) {
      statusElement.textContent = unavailableMessage;
    }
  }

  async function exchangeDeliverySecret() {
    const publicPath = window.location.pathname;
    let deliverySecret = window.location.hash;
    if (deliverySecret.startsWith("#")) {
      deliverySecret = deliverySecret.slice(1);
    } else {
      deliverySecret = "";
    }

    try {
      window.history.replaceState(null, "", publicPath);
    } catch {
      deliverySecret = "";
      try {
        window.location.replace(publicPath);
      } catch {
        showUnavailable();
      }
      return;
    }

    const publicPathMatch =
      /^\/d\/([A-Za-z0-9_-]{22})$/.exec(publicPath);
    if (
      !publicPathMatch
      || !/^[A-Za-z0-9_-]{43}$/.test(deliverySecret)
    ) {
      deliverySecret = "";
      showUnavailable();
      return;
    }

    try {
      const exchangeRequest = window.fetch(`${publicPath}/exchange`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({secret: deliverySecret}),
      });
      deliverySecret = "";

      const response = await exchangeRequest;
      if (!response.ok) {
        showUnavailable();
        return;
      }

      window.location.reload();
    } catch {
      deliverySecret = "";
      showUnavailable();
    }
  }

  exchangeDeliverySecret();
})();
